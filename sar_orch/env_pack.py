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

worker 侧（P4-3 落地）：``build_worker_tools``（``SAR_WORKER_TOOLS`` 注册表 +
运行时依赖 + Map Agent MCP 装载）/ ``build_worker_state_provider``
（``SARWorkerStateProvider``）/ ``attach_worker_runtime``（``WorkerReportPublisher``
观测发布器）/ ``worker_capabilities`` / ``format_worker_action``——自
``sar_orch/worker.py`` 逐字迁入。
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

#: SAR worker AgentCard 能力标签（P4-3 前硬编码于 sar_orch/worker.py::start()）。
_SAR_WORKER_CAPABILITIES = ["sar", "navigation", "rescue", "firefighting"]

#: worker 工具名 → SAR 动作名别名映射（P4-3 前为 sar_orch/worker.py::_build_action
#: 内的就地 name_map，逐字迁入；供 ``agent_interactions.csv`` 的 Action 标签使用）。
_WORKER_ACTION_ALIASES = {
    "navigate_to": "NavigateTo",
    "move": "Move",
    "explore": "Explore",
    "carry_person": "CarryPerson",
    "drop_off_person": "DropOffPerson",
    "get_supply": "GetSupply",
    "store_supply": "StoreSupply",
    "use_supply": "UseSupply",
    "clear_inventory": "ClearInventory",
    "no_op": "NoOp",
}


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

    # ── 2. worker 工具注册表 + 运行期附属面（P4-3 消费）─────────────────

    async def build_worker_tools(self, ctx):
        """构建 SAR worker 完整工具列表（注册表 + 运行时依赖 + MCP 装载）。

        自 ``sar_orch/worker.py::_assemble_tools_async`` 逐字迁入（P4-3）：
        分类构造语义不变——``ReportObservationTool`` 注入 agent/step、无依赖工具
        空构造、``ReadMailboxTool``/``A2ASendMailTool`` 按 peer-mail 存储有无
        条件创建、其余工具注入 ``barrier + agent_idx``；Map Agent MCP 工具装载
        失败容错（告警并继续）与现状一致。
        """
        from Agent.worker_agent.tools.mcp_loader import load_mcp_tools_async
        from sar_orch.tools.worker import SAR_WORKER_TOOLS
        from sar_orch.worker_mcp_config import write_worker_mcp_config

        tools = []
        for tool_cls in SAR_WORKER_TOOLS:
            if tool_cls.__name__ == "ReportObservationTool":
                tools.append(
                    tool_cls(
                        agent_name=ctx.agent_name,
                        task_id=ctx.current_task_id,
                        get_step=lambda: getattr(ctx.barrier, "_step_counter", 0),
                    )
                )
            elif tool_cls.__name__ in ("FinishTaskTool", "AskCoordinatorTool"):
                tools.append(tool_cls())
            elif tool_cls.__name__ == "ReadMailboxTool":
                if ctx.mailbox_store is not None:
                    tools.append(tool_cls(mailbox=ctx.mailbox_store))
                else:
                    logger.debug("ReadMailboxTool not created - peer mail disabled")
            elif tool_cls.__name__ == "A2ASendMailTool":
                if ctx.peer_sender is not None:
                    tools.append(tool_cls(sender=ctx.peer_sender))
                else:
                    logger.info("A2ASendMailTool not created -- peer mail disabled")
            else:
                tools.append(tool_cls(barrier=ctx.barrier, agent_idx=ctx.agent_idx))

        # Load Map Agent MCP tools
        mcp_log_dir = ctx.log_dir or str(Path.cwd() / "logs")
        mcp_config_path = write_worker_mcp_config(
            log_dir=mcp_log_dir,
            agent_name=ctx.agent_name,
            coordinator_http_url=ctx.http_url,
        )
        try:
            mcp_tools = await load_mcp_tools_async(
                str(mcp_config_path), connection_registry=ctx.mcp_registry
            )
            tools.extend(mcp_tools)
            logger.info(
                "Loaded %d MCP tools from map_agent: %s",
                len(mcp_tools),
                [t.name for t in mcp_tools],
            )
        except Exception:
            logger.warning(
                "Failed to load MCP tools from map_agent "
                "(will continue without MCP tools)",
                exc_info=True,
            )

        return tools

    def build_worker_state_provider(self, ctx):
        """构建 ``SARWorkerStateProvider``（自 worker.start() 逐参迁入，P4-3）。"""
        from sar_orch.worker_state_provider import SARWorkerStateProvider

        provider = SARWorkerStateProvider(
            barrier=ctx.barrier,
            agent_idx=ctx.agent_idx,
            semantic_map_url=ctx.http_url,
            mailbox=ctx.mailbox_store,
            team_state=ctx.team_state_store,
            coordinator_id=ctx.coordinator_id,
            coordinator_secret=ctx.coordinator_secret,
            # Phase 4: authenticated read-port provider; in read_port mode the
            # worker fetches /environment-state instead of constructing a
            # global-map direct-read view.
            environment_state_url=ctx.http_url,
            memory_read_mode=ctx.memory_read_mode,
            # Phase 4 (H2): rollback audit + HTTP request budget.  token_limit
            # must match the worker ContextManager token_limit (80000) so the
            # /environment-state request carries a nonzero usable budget.
            log_dir=ctx.log_dir,
            token_limit=80000,
        )
        # Phase 4: inject agent name for team status fetching
        provider._agent_name = ctx.agent_name
        return provider

    def attach_worker_runtime(self, ctx) -> None:
        """装配 SAR 观测发布器（``WorkerReportPublisher`` + ``set_publisher``）。

        自 ``sar_orch/worker.py::start()`` 逐参迁入（P4-3）。
        """
        from sar_orch.map import WorkerReportPublisher
        from sar_orch.tools.worker._barrier_helpers import set_publisher

        _publisher_inst = WorkerReportPublisher(
            agent_name=ctx.agent_name,
            step_provider=lambda: getattr(ctx.barrier, "_step_counter", 0),
        )
        set_publisher(_publisher_inst)

    @property
    def worker_capabilities(self) -> list[str]:
        """SAR worker AgentCard 能力标签（迁移前 ``worker.start()`` 内就地常量）。"""
        return list(_SAR_WORKER_CAPABILITIES)

    def format_worker_action(self, tool_name: str, args: dict) -> str:
        """SAR ``Action`` 标签（自 ``sar_orch/worker.py::_build_action`` 逐字迁入）。

        迁移前逻辑：工具名经 ``_WORKER_ACTION_ALIASES`` 映射为 SAR 动作名，
        参数按 ``str(v)`` 以 ``", "`` 连接；无参数 → ``Name()``。
        """
        sar_name = _WORKER_ACTION_ALIASES.get(tool_name, tool_name)
        if not args:
            return f"{sar_name}()"
        arg_parts = ", ".join(str(v) for v in args.values())
        return f"{sar_name}({arg_parts})"

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
