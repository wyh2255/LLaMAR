"""SAR 环境包 —— EnvPack 契约的 SAR 实现（env-contract P4-2）。

本模块承载从 ``sar_orch/coordinator.py`` 迁出的 SAR 特化件（coordinator 侧已接线）：

- 观察源：``SemanticMapStore`` + SAR priors（纯 Worker 证据合成，H1-INV-1）；
- 域摘要器：``MapSummarizer``（semantic 模式，含 token 记账）；
- state provider：``SARCoordinatorStateProvider``；
- coordinator 工具：oracle 模式 ``QuerySARStateTool``；
- 附属 LLM：Map Agent ``llm_query`` 的 ChatOpenAI + token sink；
- 内核 P2b 注入口直通工厂（``build_finish_task_tool`` /
  ``build_environment_state_provider`` / ``build_map_agent_session_lifecycle``）——
  ``sar_orch.coordinator`` 仍 re-export，既有 import 面零变化。

worker 侧工厂（``build_worker_tools`` / ``build_worker_state_provider`` /
``attach_worker_runtime``）随 P4-3（worker 抽取卡）自 ``sar_orch/worker.py`` 迁入。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from orchestration.env_pack import EnvPack
from sar_orch.map_agent import mount_to_fastapi

logger = logging.getLogger(__name__)

#: Map Agent 附属 LLM 的默认 token 记账 agent 名（与 P4-2 前一致）。
_MAP_AGENT = "MapAgent"
#: MapSummarizer 附属 LLM 的默认 token 记账 agent 名（与 P4-2 前一致）。
_MAP_SUMMARIZER = "MapSummarizer"

#: SAR semantic map 先验（P4-2 前硬编码于 coordinator.start()，逐字迁入）。
_SAR_PRIOR_RULES = {"Chemical": "Sand", "Non-chemical": "Water"}
_SAR_TASK_OBJECTIVE = "Extinguish all fires and rescue all persons"


def build_finish_task_tool(store, *, completion_validator=None):
    """SAR 侧 mission 完成工具工厂（内核 executor 注入口）。

    内核不再 import SAR ``FinishTaskTool``；SAR 装配经
    ``create_server(finish_task_tool_factory=...)`` 注入本工厂，实例构造与
    注入前完全一致（同一类、同一参数）。
    """
    from sar_orch.tools.coordinator.finish_task import FinishTaskTool

    return FinishTaskTool(store, completion_validator=completion_validator)


def build_environment_state_provider(
    *,
    memory_store,
    active_runtime,
    scope_id: str,
    worker_id: str,
    dispatch_id: str,
):
    """SAR 侧 ``/environment-state`` provider 工厂（内核路由注入口）。

    组合与内核路由原先的就地构造逐参数一致：worker 视角
    ``EnvironmentStateProvider``（MemoryReadPort over canonical store +
    ControlPlaneReadPort over active MissionRuntime）。
    """
    from sar_orch.environment_state_provider import (
        ControlPlaneReadPort,
        EnvironmentStateProvider,
        MemoryReadPort,
    )

    return EnvironmentStateProvider(
        MemoryReadPort(memory_store, scope_id),
        ControlPlaneReadPort(active_runtime),
        scope_id=scope_id,
        viewer_role="worker",
        viewer_id=worker_id,
        current_dispatch_id=dispatch_id,
    )


def build_map_agent_session_lifecycle():
    """SAR 侧 MCP 会话生命周期 provider（内核 lifespan 注入口）。

    返回 Map Agent streamable-HTTP 会话管理器的 ``run()`` 上下文，由内核
    ``lifespan`` 启动段进入、收尾段对称退出——与内核注入化之前逐字等价：
    同一 ``mcp.session_manager.run()``、同一进入/退出时机。

    容错（现状语义搬移）：``session_manager`` 由 ``mount_to_fastapi`` 惰性
    创建（``streamable_http_app()``）；未挂载语义地图时属性访问抛
    ``RuntimeError``——旧内核在 ``(ImportError, RuntimeError)`` 静默放过，
    现由本 provider 复现同一容错并返回 ``None``（内核约定 None = 本次不进入
    会话）。导入失败同理（SAR 装配路径下实际不可达，保持旧网不丢）。
    """
    try:
        from sar_orch.map_agent.server import mcp as _map_agent_mcp

        return _map_agent_mcp.session_manager.run()
    except (ImportError, RuntimeError):
        return None


def _side_token_sink(*, barrier, exp_logger, default_agent: str):
    """构造附属 LLM 的 token 记账 sink（MapSummarizer / MapAgent 共用）。

    与 P4-2 前 ``sar_orch/coordinator.py`` 的两份就地闭包逐行等价：step 取
    ``barrier._step_counter``（barrier 为 None → 0），``log_token_usage`` +
    ``flush_summary``；``exp_logger`` 为 None 时静默跳过。
    """

    def _sink(**kwargs):
        step = getattr(barrier, "_step_counter", 0) if barrier is not None else 0
        if exp_logger is not None:
            exp_logger.log_token_usage(
                step=step,
                agent=kwargs.get("agent", default_agent),
                prompt_tokens=kwargs.get("prompt_tokens", 0),
                completion_tokens=kwargs.get("completion_tokens", 0),
                total_tokens=kwargs.get("total_tokens", 0),
                cache_hit_tokens=kwargs.get("cache_hit_tokens", 0),
                cache_miss_tokens=kwargs.get("cache_miss_tokens", 0),
            )
            exp_logger.flush_summary()

    return _sink


class SAREnvPack(EnvPack):
    """SAR 环境包。

    构造参数由 SAR 装配层传入；``None`` 语义 = 保持现状默认：

    - ``coordinator_prompts_dir``：coordinator prompts 根（``None`` = 内核默认
      发现，P4-1 语义）；
    - ``worker_prompts_dir``：worker prompts 根（P4-3 消费）；
    - ``map_summary_path``：``MapSummarizer`` 产物路径（``None`` = 不启用）；
    - ``ui_dir``：控制台静态目录（``None`` = ``sar_orch/ui`` 现状默认）。
    """

    name = "sar"

    def __init__(
        self,
        *,
        coordinator_prompts_dir: str | None = None,
        worker_prompts_dir: str | None = None,
        map_summary_path: str | Path | None = None,
        ui_dir: str | None = None,
    ) -> None:
        self._coordinator_prompts_dir = coordinator_prompts_dir
        self._worker_prompts_dir = worker_prompts_dir
        self._map_summary_path = Path(map_summary_path) if map_summary_path else None
        self._ui_dir = ui_dir

    # ── 1. barrier 工厂（装配层 P4-4 消费；与 experiment.py 现状逐参等价）──

    def build_barrier(self, *, num_agents: int, seed: int, **env_params):
        from sar_orch.barrier import SARBarrier

        if "scene" not in env_params:
            raise TypeError("SAREnvPack.build_barrier requires scene=<int>")
        scene = env_params.pop("scene")
        if env_params:
            raise TypeError(
                f"SAREnvPack.build_barrier got unexpected env_params: "
                f"{sorted(env_params)}"
            )
        return SARBarrier(num_agents=num_agents, scene=scene, seed=seed)

    # ── 3. coordinator 工具工厂 + 内核注入口直通（P4-2 消费）─────────────

    def build_coordinator_tools(self, ctx) -> list:
        # oracle 模式注册 QuerySARStateTool（与 P4-2 前 coordinator.start()
        # 的条件逐字一致）；semantic 模式状态经 state provider 自动注入。
        if ctx.state_mode != "oracle":
            return []
        from sar_orch.tools.coordinator import QuerySARStateTool

        return [QuerySARStateTool(ctx.barrier)]

    # 内核注入口直通（P2b 工厂）——staticmethod 包装保证实例访问返回模块级
    # 函数本体（identity 与 ``sar_orch.coordinator`` re-export 一致）。
    finish_task_tool_factory = staticmethod(build_finish_task_tool)
    environment_state_provider_factory = staticmethod(build_environment_state_provider)
    map_mcp_mount_hook = staticmethod(mount_to_fastapi)
    mcp_session_lifecycle_provider = staticmethod(build_map_agent_session_lifecycle)

    # ── 4. coordinator state provider 工厂（P4-2 消费）──────────────────

    def build_coordinator_state_provider(self, ctx):
        from sar_orch.coordinator_state_provider import SARCoordinatorStateProvider

        diag_config = ctx.diagnosis_config
        return SARCoordinatorStateProvider(
            barrier=ctx.barrier,
            semantic_map=ctx.observation_source,
            event_store=ctx.event_store,
            state_mode=ctx.state_mode,
            supervision_state_store=ctx.supervision_state_store,
            map_summarizer=ctx.domain_summarizer,
            log_dir=ctx.log_dir,
            user_command_queue=ctx.user_command_queue,
            memory_read_mode=ctx.memory_read_mode,
            # Phase 5 #2: pass the long-term store + mode through to the
            # read-port provider (mode=off → store is None → provider stays
            # on its default off path).
            long_term_mode=ctx.long_term_mode,
            long_term_store=ctx.long_term_store,
            # Phase 4 (P4): pass the diagnosis store + injection knob
            # through to the read-port provider (None store → the
            # system_health section never materializes).
            diagnosis_store=ctx.diagnosis_store,
            diagnosis_inject_enabled=bool(
                diag_config is not None and diag_config.inject_enabled
            ),
            diagnosis_min_confidence=(
                diag_config.min_confidence if diag_config is not None else 0.6
            ),
            # R3 修订: system_health 预算档透传（诊断通道未启用时保持默认 3）。
            diagnosis_budget_threshold=(
                diag_config.section_budget_threshold if diag_config is not None else 3
            ),
        )

    # ── 5. Context·session 工厂（P4-2 coordinator 侧消费）──────────────

    def build_session_factory(self, *, role: str):
        # SAR 无 Context 子类：两类角色都走内核缺省 factory（P4-1 逐字等价）。
        return None

    # ── 6. prompts 目录（装配层消费）───────────────────────────────────

    @property
    def coordinator_prompts_dir(self) -> str | None:
        return self._coordinator_prompts_dir

    @property
    def coordinator_skills_dir(self) -> str | None:
        # 现状约定：<prompts 根>/../../skills/coordinator（coordinator.py 原式）。
        if not self._coordinator_prompts_dir:
            return None
        return str(
            Path(self._coordinator_prompts_dir).parent.parent / "skills" / "coordinator"
        )

    @property
    def worker_prompts_dir(self) -> str | None:
        return self._worker_prompts_dir

    @property
    def worker_skills_dir(self) -> str | None:
        # 现状约定：<prompts 根>/../../skills/worker（worker.py 原式，P4-3 消费）。
        if not self._worker_prompts_dir:
            return None
        return str(Path(self._worker_prompts_dir).parent.parent / "skills" / "worker")

    # ── 7. 观察与域数据钩子（P4-2 消费）────────────────────────────────

    def build_observation_source(self, ctx):
        """构建 SAR 观测源：``SemanticMapStore``（纯 Worker 证据合成）。

        Phase 3 (H1-INV-1)：在线语义地图绝不从仿真器/Barrier 场景先验或
        checker 真值播种——水库/沉积/火点/人员/agent 位置全部经认证的 Worker
        观测发现；地图从空开始。priors 只含 agent 名与规则常量（与 P4-2 前
        ``coordinator.start()`` 逐参数一致）。
        """
        from sar_orch.map import SemanticMapStore

        semantic_map = SemanticMapStore()
        semantic_map.set_jsonl_path(
            Path(ctx.log_dir) / "semantic_map.jsonl" if ctx.log_dir else None
        )
        _agent_names = (
            getattr(getattr(ctx.barrier, "env", None), "agent_names", [])
            if ctx.barrier is not None
            else []
        )
        semantic_map.init_priors(
            reservoirs=[],
            deposits=[],
            agents=[{"agent_id": name} for name in _agent_names],
            rules=dict(_SAR_PRIOR_RULES),
            step_budget={
                "current_step": 0,
                "max_steps": ctx.max_steps,
                "remaining": ctx.max_steps,
            },
            task_objective=_SAR_TASK_OBJECTIVE,
        )
        return semantic_map

    def build_domain_summarizer(self, ctx):
        """构建 ``MapSummarizer``（semantic 模式 + 路径/logger 齐备时）。

        启用条件与告警文本与 P4-2 前 ``coordinator.start()`` 逐字一致：
        ``state_mode != "semantic"`` → 静默返回 None；路径或 exp_logger 缺失
        → 告警 + None。
        """
        if ctx.state_mode != "semantic":
            return None
        if self._map_summary_path is None or ctx.exp_logger is None:
            logger.warning(
                "MapSummarizer disabled: %s %s",
                "no map_summary_path" if self._map_summary_path is None else "",
                "no exp_logger" if ctx.exp_logger is None else "",
            )
            return None

        from sar_orch.map.summarizer import MapSummarizer

        return MapSummarizer(
            summary_path=self._map_summary_path,
            token_usage_sink=_side_token_sink(
                barrier=ctx.barrier,
                exp_logger=ctx.exp_logger,
                default_agent=_MAP_SUMMARIZER,
            ),
            summary_timeout_seconds=30.0,
        )

    # ── 8. 日志与产物配置 ──────────────────────────────────────────────

    @property
    def ui_dir(self) -> str | None:
        if self._ui_dir is not None:
            return self._ui_dir
        # 现状默认：SAR UI 静态文件随编排代码存放（sar_orch/ui/）。
        return str(Path(__file__).resolve().parent / "ui")

    # ── 9. 附属 LLM 接线（可选）───────────────────────────────────────

    def attach_auxiliary_llm(self, ctx) -> None:
        """Phase 3: 注入 LLM client + token sink 到 Map Agent（llm_query）。

        与 P4-2 前 ``coordinator.start()`` 的就地装配逐参一致：ChatOpenAI
        （model / api_base / api_key_env 来自运行时配置，temperature=0.0）。
        """
        from langchain_openai import ChatOpenAI

        from sar_orch.map_agent import set_llm_client, set_token_sink

        _map_agent_llm = ChatOpenAI(
            model=ctx.model,
            openai_api_key=os.environ.get(ctx.api_key_env, ""),
            openai_api_base=ctx.api_base,
            temperature=0.0,
        )
        set_llm_client(_map_agent_llm)
        set_token_sink(
            _side_token_sink(
                barrier=ctx.barrier,
                exp_logger=ctx.exp_logger,
                default_agent=_MAP_AGENT,
            )
        )
