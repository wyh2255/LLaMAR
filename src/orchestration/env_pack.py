"""EnvPack —— 环境包契约（env-contract P4-2 定型；形式 = ABC + 工厂签名）。

本模块定义「通用编排层 ↔ 环境包」之间的唯一契约面（``EnvPack``）。通用编排骨架
（``orchestration.coordinator`` / ``orchestration.worker``）只经本契约访问环境能力；
环境包（``sar_orch`` / ``ai2thor_orch``）实现本契约；内核（``src/Agent`` + ``src/a2a``）
保持零环境引用。

依赖方向（硬不变量）::

    环境包 <env>_orch/ ──依赖──▶ 内核 src/Agent + src/a2a ◀──依赖── 编排层（经 EnvPack 注入）

守卫测试：``tests/test_kernel_dependency_direction.py``（内核零 ``*_orch`` 引用）与
``tests/test_orchestration_dependency_direction.py``（本包零环境包引用）。

设计纪律（薄抽象，env_contract.md §6-1）：契约只收「已出现的差异」——以 SAR 与
AI2Thor 双方真实存在的缝为准；不为想象中的第 N 个环境预留抽象。

── 契约面（对齐 env_contract.md §2.1 的 9 类；括注消费方/阶段）──────────────

1. barrier 工厂（装配层 P4-4 消费）
   ``build_barrier(*, num_agents, seed, **env_params)``。
   SAR：``SARBarrier(num_agents, scene, seed)``；AI2Thor：``AI2ThorBarrier(num_agents,
   executor, max_steps, alias_registry)``（随 P5 接线）。返回对象必需表面：
   ``submit_action(agent_idx, action, *, advance=True, source=None)`` / ``stop()`` /
   ``request_stop(reason)`` / ``get_run_status()`` / ``is_finished()`` /
   ``_step_counter``（回合语义与线程模型见 env_contract.md §2.2-①②⑤⑦）。

2. worker 工具注册表 + worker 运行期附属面（P4-3 消费）
   ``async build_worker_tools(ctx: WorkerEnv) -> list``：目录型注册表（如
   ``SAR_WORKER_TOOLS``）+ 运行时依赖装配（barrier / agent_idx / mailbox / sender）
   + 可选 MCP 工具装载，全部环境侧实现。义务：必须含任务完成工具
   （``ToolResult(task_complete=True)``；``require_explicit_completion=True``
   时缺它任务无法正常终结）。
   ``attach_worker_runtime(ctx)``：state provider 构造后、服务创建前的运行期接线
   （SAR = ``WorkerReportPublisher`` 观测发布器）。
   ``worker_capabilities``：worker AgentCard 能力标签（A2A 注册面）。
   ``format_worker_action(tool_name, args)``：``agent_interactions.csv`` 的
   ``Action`` 标签格式化（SAR = 域别名映射 ``navigate_to`` → ``NavigateTo``）。

3. coordinator 工具工厂（P4-2 coordinator 骨架消费）
   ``build_coordinator_tools(ctx) -> list``（SAR：oracle 模式的 ``QuerySARStateTool``）。
   ``finish_task_tool_factory`` / ``environment_state_provider_factory`` /
   ``map_mcp_mount_hook`` / ``mcp_session_lifecycle_provider``：内核 create_server
   注入口直通（P2b 已注入化，语义见 env_contract.md §2.1-3 / §2.1-+）。

4. state provider 工厂（P4-2 / P4-3 消费）
   ``build_coordinator_state_provider(ctx)`` / ``build_worker_state_provider(ctx)``。
   契约：``snapshot() -> RuntimeState``（``version`` 单调，变化才刷新；可选
   ``AsyncStatePreparer.prepare_for_llm``）。coordinator 侧 provider 还须支持
   ``set_memory_ingestor(ingestor)``（canonical Memory 读端口注入，P2 现状）。

5. Context·session 工厂（P4-2 coordinator 侧 / P4-3 worker 侧消费）
   ``build_session_factory(*, role) -> Callable[[], Any] | None``；
   ``None`` = 内核缺省（``CoordinatorContextManager`` / ``WorkerContextManager``，
   逐字等价）。SAR 无 Context 子类 → ``None``；AI2Thor 的
   ``AI2Thor{Coordinator,Worker}ContextManager`` 随 P5 接线。coordinator 侧经
   ``create_server(session_factory=...)`` 透传 ``CoordinatorAgentExecutor``（P4-2
   接通）；worker 侧随 P4-3 接通。

6. prompts 目录（装配层 / 内核 prompt 加载消费）
   ``coordinator_prompts_dir`` / ``coordinator_skills_dir`` /
   ``worker_prompts_dir`` / ``worker_skills_dir``。SAR 默认树内布局
   （``None`` = 现状布局，P4-1 语义）；skills 按 ``<prompts>/../../skills/<role>``
   现状约定推导。

7. 观察与域数据钩子（P4-2 coordinator 骨架消费）
   ``build_observation_source(ctx) -> Any | None``：观测摄取源（SAR =
   ``SemanticMapStore`` + SAR priors；AI2Thor 暂无 → ``None``），就绪后经内核
   ``set_semantic_map`` 接入观测摄取链。
   ``build_domain_summarizer(ctx) -> Any | None``：域摘要器（SAR = ``MapSummarizer``；
   AI2Thor 暂无 → ``None``）。

8. 日志与产物配置
   ``ui_dir``：控制台静态目录（SAR = ``sar_orch/ui``；``None`` = 内核不挂 UI 路由）。
   语义图 jsonl 路径与 map_summary 路径由第 7 类工厂落定；``log_dir`` /
   ``supervision_dir`` / ``exp_logger`` 为运行时装配参数（内核与装配层注入，
   本契约不重定义）。

9. 附属 LLM 接线（可选；P4-2 coordinator 骨架消费）
   ``attach_auxiliary_llm(ctx)``：SAR 为 Map Agent ``llm_query`` 装配 ChatOpenAI +
   token sink（``set_llm_client`` / ``set_token_sink``）；缺省 no-op。

── 分阶段状态（2026-09-13，P4-3 时点）─────────────────────────────────────

- SAR：九类全部实现并接线——第 3/4(coordinator)/5(coordinator)/6/7/8/9 类 P4-2；
  第 2/4(worker)/5(worker) 类 P4-3（worker 工具注册表 / ``SARWorkerStateProvider`` /
  worker session 工厂 / 运行期接线 / 能力标签 / Action 格式化）。第 1 类（barrier
  工厂）随 P4-4 装配层迁入后消费。
- AI2Thor：整包随 P5 新增 ``ai2thor_orch`` EnvPack 实现。
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class CoordinatorEnv:
    """``OrchestratorCoordinator.start()`` 装配期传给 EnvPack 工厂的依赖快照。

    字段分两类：

    - **内核侧装配产物**（骨架填充，只读使用）；
    - **前序工厂产物**（``observation_source`` / ``domain_summarizer``）——骨架在
      调用对应工厂前回填；后序工厂（如 state provider 工厂）可读取。
    """

    # 运行参数
    barrier: Any
    log_dir: str | None
    run_id: str
    state_mode: str
    max_steps: int
    model: str
    api_base: str
    api_key_env: str

    # 内核侧装配产物
    exp_logger: Any
    event_store: Any
    supervision_state_store: Any
    user_command_queue: Any
    memory_read_mode: str
    long_term_mode: str
    long_term_store: Any
    diagnosis_store: Any
    diagnosis_config: Any

    # 前序工厂产物（按序回填）
    observation_source: Any = None
    domain_summarizer: Any = None


@dataclass
class WorkerEnv:
    """worker 装配期传给 EnvPack worker 侧工厂的依赖快照（P4-3 消费）。

    字段以 ``orchestration/worker.py`` 骨架装配时已解析的运行时事实为准（凭据
    解析、peer-mail 存储、coordinator_id 派生、http_url 换算之后的快照）；新增
    字段只增不改。
    """

    worker_id: str
    agent_name: str
    agent_idx: int
    barrier: Any
    log_dir: str | None
    model: str
    api_base: str
    api_key_env: str
    memory_read_mode: str
    exp_logger: Any
    coordinator_url: str
    http_url: str
    coordinator_secret: bytes | None
    coordinator_id: str = "Coordinator"
    prune_policy: str = "count_window"
    mailbox_store: Any = None
    team_state_store: Any = None
    peer_sender: Any = None
    current_task_id: str = ""
    mcp_registry: Any = None


class EnvPack(ABC):
    """环境包契约：编排层与内核之外的全部环境能力供给面。

    实现者：各环境包（``sar_orch.env_pack.SAREnvPack``；AI2Thor 随 P5）。
    消费方：通用编排骨架（coordinator 侧 = ``OrchestratorCoordinator``；worker 侧
    随 P4-3）。方法默认实现保证「薄环境包」可仅实现所需面；标
    ``NotImplementedError`` 的为对应阶段的必供项。
    """

    #: 环境标识（用于日志与产物命名，如 ``"sar"``）。
    name: str = ""

    # ── 1. barrier 工厂（装配层消费）────────────────────────────────────

    def build_barrier(self, *, num_agents: int, seed: int, **env_params: Any) -> Any:
        """构造环境回合同步 barrier（生命周期 + 回合语义 + run control 载体）。

        ``env_params`` 为环境私有参数（SAR：``scene=``；AI2Thor：``executor=``、
        ``max_steps=`` 等）。返回对象的必需表面见模块 docstring 第 1 类。
        """
        raise NotImplementedError(f"EnvPack[{self.name or '?'}].build_barrier 未实现")

    # ── 2. worker 工具注册表 + worker 运行期附属面（P4-3 消费）──────────

    async def build_worker_tools(self, ctx: WorkerEnv) -> list[Any]:
        """构造 worker 的完整工具列表（注册表 + 运行时依赖装配 + MCP 装载）。

        P4-3 已落地；SAR 实现自 ``sar_orch/worker.py`` 的 ``_assemble_tools_async``
        迁入（``SAREnvPack``）。
        """
        raise NotImplementedError(
            f"EnvPack[{self.name or '?'}].build_worker_tools 未实现"
        )

    def attach_worker_runtime(self, ctx: WorkerEnv) -> None:
        """worker 运行期接线钩子（state provider 构造后、服务创建前调用）。

        SAR = ``WorkerReportPublisher`` 观测发布器装配（``set_publisher``）；
        缺省 no-op。
        """
        return

    @property
    def worker_capabilities(self) -> list[str]:
        """worker AgentCard 能力标签（A2A 注册面）；缺省空列表。"""
        return []

    def format_worker_action(self, tool_name: str, args: dict) -> str:
        """``tool_result`` → ``agent_interactions.csv`` 的 ``Action`` 标签。

        缺省 = 工具名直出；环境域别名映射（SAR：``navigate_to`` → ``NavigateTo``）
        由环境包覆写。输出为实验产物字段，环境包须逐字复现迁移前格式。
        """
        if not args:
            return f"{tool_name}()"
        return f"{tool_name}({', '.join(str(v) for v in args.values())})"

    # ── 3. coordinator 工具工厂 + 内核注入口直通（P4-2 消费）─────────────

    def build_coordinator_tools(self, ctx: CoordinatorEnv) -> list[Any]:
        """构造 coordinator 运行时工具列表（经 ``create_server(extra_tools=)`` 注入）。"""
        return []

    #: ``finish_task_tool_factory(store, *, completion_validator) -> Tool``（内核注入口直通）。
    finish_task_tool_factory: Callable[..., Any] | None = None
    #: ``environment_state_provider_factory(*, memory_store, active_runtime, scope_id,
    #: worker_id, dispatch_id) -> provider``（内核注入口直通）。
    environment_state_provider_factory: Callable[..., Any] | None = None
    #: ``map_mcp_mount_hook(app, semantic_map) -> None``（内核注入口直通）。
    map_mcp_mount_hook: Callable[..., Any] | None = None
    #: ``mcp_session_lifecycle_provider() -> async context manager | None``（内核注入口直通）。
    mcp_session_lifecycle_provider: Callable[..., Any] | None = None

    # ── 4. state provider 工厂（P4-2 / P4-3 消费）──────────────────────

    def build_coordinator_state_provider(self, ctx: CoordinatorEnv) -> Any:
        """构造 coordinator 侧 state provider（``snapshot() -> RuntimeState``）。"""
        raise NotImplementedError(
            f"EnvPack[{self.name or '?'}].build_coordinator_state_provider 未实现"
        )

    def build_worker_state_provider(self, ctx: WorkerEnv) -> Any:
        """构造 worker 侧 state provider（P4-3 落地；``SARWorkerStateProvider`` 迁入处）。"""
        raise NotImplementedError(
            f"EnvPack[{self.name or '?'}].build_worker_state_provider 未实现"
        )

    # ── 5. Context·session 工厂（P4-2 / P4-3 消费）─────────────────────

    def build_session_factory(self, *, role: str) -> Callable[[], Any] | None:
        """返回对应角色的 session 工厂（``role`` ∈ ``{"coordinator", "worker"}``）。

        ``None`` = 内核缺省 factory（逐字等价，见 P4-1）。
        """
        return None

    # ── 6. prompts 目录（装配层消费）───────────────────────────────────

    @property
    def coordinator_prompts_dir(self) -> str | None:
        """coordinator prompts 根目录；``None`` = 内核默认发现（现状布局）。"""
        return None

    @property
    def coordinator_skills_dir(self) -> str | None:
        """coordinator skills 目录；``None`` = 不注入。"""
        return None

    @property
    def worker_prompts_dir(self) -> str | None:
        """worker prompts 根目录；``None`` = 内核默认发现（现状布局）。"""
        return None

    @property
    def worker_skills_dir(self) -> str | None:
        """worker skills 目录；``None`` = 不注入。"""
        return None

    # ── 7. 观察与域数据钩子（P4-2 消费）────────────────────────────────

    def build_observation_source(self, ctx: CoordinatorEnv) -> Any | None:
        """构造观测摄取源；``None`` = 本环境无观测源（内核不接 ``set_semantic_map``）。"""
        return None

    def build_domain_summarizer(self, ctx: CoordinatorEnv) -> Any | None:
        """构造域摘要器（可读 ``ctx.observation_source``）；``None`` = 无摘要器。"""
        return None

    # ── 8. 日志与产物配置 ──────────────────────────────────────────────

    @property
    def ui_dir(self) -> str | None:
        """控制台静态目录；``None`` = 内核不挂 UI 路由。"""
        return None

    # ── 9. 附属 LLM 接线（可选）───────────────────────────────────────

    def attach_auxiliary_llm(self, ctx: CoordinatorEnv) -> None:
        """附属 LLM 接线钩子（server 创建后调用；SAR = Map Agent llm_query）。"""
        return
