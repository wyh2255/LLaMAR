"""通用实验装配骨架（env-contract P4-4）。

把 SAR / AI2Thor 两环境共享的「一次实验的装配与运维」抽到本模块：

- 实验目录布局（``coordinator/`` / ``workers/<Agent>/`` / ``supervision/``）；
- barrier 构造（``EnvPack.build_barrier`` 工厂，第 1 类契约的第一消费点）；
- ExperimentLogger 接线（run context / metadata / 每步落盘 / 收尾关闭）；
- sandbox 策略与 per-run coordinator secret；
- coordinator / worker 服务启动与等待（线程与 asyncio task 生命周期）；
- 初始任务提交 + barrier poll 循环（步数预算 + wall-clock 兜底 + 每步日志）；
- 终局判定（``classify_end_reason``）、终态产物冻结与收尾（停机 + logger 关闭）。

环境特化面经两处注入（本模块零环境实现）：

- ``orchestration.env_pack.EnvPack``：barrier 工厂、prompts/skills 目录、
  工具 / state provider / session 工厂等环境能力（P4-1~P4-3 已定型）；
- ``AssemblyHooks``（本模块定义）：装配期生命周期钩子，环境侧按需覆写——
  环境产物快照、每步域指标（如 SAR 语义地图 recall/freshness）、终态评测等。
  缺省实现全部 no-op / 空返回，因此一个最小 EnvPack + 缺省钩子即可跑通装配。

依赖方向（硬不变量）：本模块只依赖内核（``src/Agent`` + ``src/a2a``）与
``orchestration`` 包内模块；守卫测试
``tests/test_orchestration_dependency_direction.py``。

── logger 契约（装配层消费）──────────────────────────────────────────────

``AssemblySpec.logger_factory(log_dir)`` 返回的实验日志对象需提供：
``set_run_context(run_id=, model=, prompt_version=)`` / ``write_metadata(dict)`` /
``log_step(**kwargs)``（SAR schema：step_num/actions/successes/observations/
coverage/transport_rate/finished/timeout_agents/noop_sources/map_recall/freshness/
run_id/max_steps/remaining_steps/wall_time_since_start/step_duration_ms/
error_types/completed_subtasks_delta/end_reason）/ ``flush_summary()`` /
``set_end_reason(str)`` / ``get_log_dir()`` / ``close()``，可选 ``freeze_terminal()``。
``logger_factory=None`` 时使用内置 ``NullExperimentLogger``（不落 CSV，仅保持
装配流程可跑——用于无 CSV 产物的环境或最小装配测试）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from a2a.shared.env_loader import load_env_file
from Agent.sandbox import SandboxPolicy
from orchestration.coordinator import OrchestratorCoordinator
from orchestration.env_pack import EnvPack
from orchestration.worker import OrchestratorWorker

logger = logging.getLogger("orchestration.assembly")


def classify_end_reason(
    *,
    finished: bool,
    steps: int,
    max_steps: int,
    elapsed_seconds: float,
    wall_clock_limit: float,
    a2a_done: bool,
    a2a_error: bool,
    coordinator_error: bool,
) -> str:
    """把一次 run 的终态归入稳定类别（产物字段 ``EndReason`` 的单一来源）。"""
    if finished:
        return "success"
    if coordinator_error or a2a_error:
        return "framework_error"
    if elapsed_seconds >= wall_clock_limit:
        return "wall_clock_timeout"
    if steps >= max_steps:
        return "max_steps_reached"
    if a2a_done:
        return "coordinator_finished_early"
    return "stopped_before_success"


class NullExperimentLogger:
    """装配层内置的最小日志对象（``logger_factory=None`` 时的缺省）。

    只实现装配层消费的接口，全部 no-op（``get_log_dir`` 返回装配目录），
    使无 CSV 产物需求的环境包也能跑通装配骨架。
    """

    def __init__(self, log_dir: str) -> None:
        self._log_dir = log_dir

    def set_run_context(self, **_kwargs) -> None:
        return

    def write_metadata(self, _metadata: dict) -> None:
        return

    def log_step(self, **_kwargs) -> None:
        return

    def flush_summary(self) -> None:
        return

    def set_end_reason(self, _reason: str) -> None:
        return

    def freeze_terminal(self) -> None:
        return

    def get_log_dir(self) -> str:
        return self._log_dir

    def close(self) -> None:
        return


@dataclass
class AssemblySpec:
    """一次实验装配的完整输入（env-agnostic）。

    环境私有参数（SAR：``scene=``）走 ``env_params`` 透传给
    ``EnvPack.build_barrier``；``env_file`` 为凭据发现面（仓库根 ``.env``），
    ``None`` = 不加载任何 env 文件。
    """

    # ── 环境与运行参数 ──────────────────────────────────────────────────
    env_pack: EnvPack
    log_dir: str
    num_agents: int
    agent_names: list[str]
    seed: int
    run_id: str = ""
    model: str = "deepseek-v4-flash"
    provider: str = "openai"
    api_base: str = "https://api.deepseek.com"
    api_key_env: str = "OPENAI_API_KEY"
    max_steps: int = 50
    wall_clock_limit: float = 3600.0
    coordinator_host: str = "0.0.0.0"
    coordinator_port: int = 8080
    agent_host: str = "0.0.0.0"
    agent_base_port: int = 8191
    orchestration_mode: str = "agentic"
    state_mode: str = "semantic"
    sandbox_profile: str = "workspace"
    project_root: str | None = None
    enable_peer_mail: bool = False
    memory_read_mode: str = "read_port"
    long_term_mode: str = "off"
    prune_policy: str = "count_window"
    prompt_version: str = "baseline"
    task_description: str = ""
    env_params: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    env_file: str | None = None

    # ── 装配节奏（等待窗口）────────────────────────────────────────────
    coordinator_settle_seconds: float = 3.0
    worker_settle_seconds: float = 5.0
    submit_settle_seconds: float = 0.5
    poll_interval: float = 2.0

    # ── 注入面 ─────────────────────────────────────────────────────────
    #: 实验日志对象工厂（``log_dir -> logger``）；``None`` = 内置 Null logger。
    logger_factory: Callable[[str], Any] | None = None
    #: coordinator/worker 构造类；``None`` = 通用骨架类（运行期解析，便于注入）。
    coordinator_factory: Callable[..., Any] | None = None
    worker_factory: Callable[..., Any] | None = None
    #: 装配生命周期钩子；``None`` = 全 no-op 基类。
    hooks: AssemblyHooks | None = None


@dataclass
class AssemblyState:
    """装配期共享状态：通用骨架填写，钩子读写。"""

    spec: AssemblySpec
    env_pack: EnvPack
    exp_dir: Path
    run_id: str
    max_steps: int
    worker_log_dirs: dict[str, str] = field(default_factory=dict)
    barrier: Any = None
    exp_logger: Any = None
    sandbox_policy: Any = None
    coordinator_secret: bytes | None = None
    coordinator: Any = None
    coord_task: Any = None
    workers: dict[str, Any] = field(default_factory=dict)
    #: 装配层从 ``env_file`` 解析出的 env 字典（钩子可读；缺省 {}）。
    env: dict = field(default_factory=dict)
    #: 钩子私有暂存（跨钩子调用传递状态，如 truth manifest）。
    scratch: dict[str, Any] = field(default_factory=dict)


class AssemblyHooks:
    """装配生命周期钩子基类（缺省全 no-op）——环境侧按需覆写。

    调用顺序：``coordinator_kwargs`` / ``worker_kwargs``（构造前）→
    ``on_environment_ready``（barrier 建好、任何回合之前）→
    ``on_coordinator_started``（coordinator 启动并等待注册后）→
    ``step_metrics`` / ``on_step``（每个新落盘回合）→ ``on_poll``（每轮 poll）→
    ``finalize_artifacts``（终局判定后、停机前）→
    ``run_terminal_evaluations``（run_metrics.json 落盘后）。
    """

    def coordinator_kwargs(self, state: AssemblyState) -> dict[str, Any]:
        """追加 coordinator 构造参数（缺省 {}）。"""
        return {}

    def worker_kwargs(self, state: AssemblyState) -> dict[str, Any]:
        """追加 worker 构造参数（缺省 {}）。"""
        return {}

    def on_environment_ready(self, state: AssemblyState) -> None:
        """环境初始化完成、任何回合执行之前（环境产物快照的落点）。"""

    def on_coordinator_started(self, state: AssemblyState) -> None:
        """coordinator 已启动并等待注册完成（运行期接线钩子的落点）。"""

    def step_metrics(
        self, state: AssemblyState, *, step_num: int, step_log: dict
    ) -> dict[str, Any]:
        """本回合的域扩展指标（并入 ``log_step`` 调用；缺省 {}）。"""
        return {}

    def on_step(
        self,
        state: AssemblyState,
        *,
        step_num: int,
        step_log: dict,
        drained_logs: list,
    ) -> None:
        """单个回合已落盘后（域状态推进 / 逐回合记录的落点）。"""

    def on_poll(
        self, state: AssemblyState, *, metrics: dict, drained_logs: list
    ) -> None:
        """每轮 poll 的收尾（不按回合去重；域级触发器落点）。"""

    def finalize_artifacts(
        self, state: AssemblyState, *, final_metrics: dict, end_reason: str
    ) -> dict[str, Any]:
        """终局判定后、停机前的产物冻结；返回 dict 并入 final_metrics。"""
        return {}

    def run_terminal_evaluations(
        self, state: AssemblyState, *, final_metrics: dict
    ) -> dict[str, Any]:
        """run_metrics.json 落盘后的终态评测；返回 dict 并入 final_metrics。"""
        return {}


def _validate_optional_dir(path: str | None, label: str) -> None:
    """EnvPack 声明的目录必须真实存在（fail-fast，禁止静默回退）。

    内核 prompt 加载对缺失目录是静默降级（加载空 system prompt）；装配层
    在此把「显式声明的路径不存在」变成硬错误——配置错误必须即刻可见。
    """
    if path is None:
        return
    if not Path(path).is_dir():
        raise FileNotFoundError(
            f"EnvPack {label} does not exist or is not a directory: {path!r} "
            f"(refusing to silently fall back to the kernel default)"
        )


def _build_sandbox_policy(spec: AssemblySpec, exp_dir: Path) -> Any:
    """按 profile 构造 sandbox 策略（与 P4-4 前 experiment.py 逐参等价）。"""
    if spec.sandbox_profile == "off":
        logger.info("Sandbox: disabled (profile=off)")
        return SandboxPolicy.off()
    if spec.sandbox_profile == "workspace":
        project_root = Path(spec.project_root) if spec.project_root else Path.cwd()
        policy = SandboxPolicy.workspace(
            project_root=project_root,
            workspace_dir="./workspace",
            write_roots=[exp_dir],
        )
        logger.info("Sandbox: workspace profile (write_roots=[%s])", exp_dir)
        return policy
    raise ValueError(
        f"Invalid sandbox profile '{spec.sandbox_profile}'. Must be 'off' or 'workspace'."
    )


def _setup_experiment_dirs(exp_dir: Path, agent_names: list[str]) -> dict[str, str]:
    """创建运行目录结构；返回 ``{agent_name: worker_log_dir}`` 映射。"""
    coord_dir = exp_dir / "coordinator"
    workers_dir = exp_dir / "workers"
    supervision_dir = exp_dir / "supervision"
    coord_dir.mkdir(exist_ok=True)
    supervision_dir.mkdir(exist_ok=True)

    worker_log_dirs: dict[str, str] = {}
    for name in agent_names:
        wdir = workers_dir / name
        wdir.mkdir(parents=True, exist_ok=True)
        worker_log_dirs[name] = str(wdir)
    return worker_log_dirs


async def run_assembly(spec: AssemblySpec) -> dict:
    """按 ``spec`` 装配并运行一次实验，返回终态 metrics 字典。

    流程与 P4-4 前 ``sar_orch/experiment.py::run_experiment`` 逐段对应：
    barrier → 目录 → logger/metadata → sandbox/secret → coordinator →
    workers → 初始任务 → poll 循环 → 终局判定 → 终态产物 → 收尾。
    """
    hooks = spec.hooks or AssemblyHooks()
    env_pack = spec.env_pack

    # G6/P4-4：prompts 目录是 EnvPack 携带的装配输入；显式声明却不存在时
    # fail-fast（内核加载器对缺失文件是静默缺省——装配层不放行这种回退）。
    _validate_optional_dir(env_pack.coordinator_prompts_dir, "coordinator_prompts_dir")
    _validate_optional_dir(env_pack.worker_prompts_dir, "worker_prompts_dir")

    logger.info("=" * 60)
    logger.info(
        "%s assembly: agents=%d, seed=%d",
        env_pack.name or "env",
        spec.num_agents,
        spec.seed,
    )
    logger.info("Model: %s (provider=%s)", spec.model, spec.provider)
    logger.info("=" * 60)

    # 1. barrier（EnvPack 第 1 类工厂；环境私有参数经 env_params 透传）
    barrier = env_pack.build_barrier(
        num_agents=spec.num_agents, seed=spec.seed, **spec.env_params
    )
    max_steps = spec.max_steps
    logger.info("%s barrier initialized -- max_steps=%d", env_pack.name or "env", max_steps)

    # 2. 实验目录（统一命名约定由调用方解析后传入）
    exp_dir = Path(spec.log_dir)
    exp_dir.mkdir(parents=True, exist_ok=True)
    worker_log_dirs = _setup_experiment_dirs(exp_dir, spec.agent_names)
    logger.info("Experiment logs unified under: %s", exp_dir)

    # 3. logger / run 上下文 / metadata
    logger_factory = spec.logger_factory or NullExperimentLogger
    exp_logger = logger_factory(str(exp_dir))
    run_id = spec.run_id or f"{env_pack.name or 'run'}-{uuid.uuid4().hex[:8]}"
    wall_clock_limit = spec.wall_clock_limit
    exp_logger.set_run_context(
        run_id=run_id, model=spec.model, prompt_version=spec.prompt_version
    )
    exp_logger.write_metadata(spec.metadata)

    state = AssemblyState(
        spec=spec,
        env_pack=env_pack,
        exp_dir=exp_dir,
        run_id=run_id,
        max_steps=max_steps,
        worker_log_dirs=worker_log_dirs,
        barrier=barrier,
        exp_logger=exp_logger,
    )

    # 4. sandbox 策略 + per-run coordinator secret
    state.sandbox_policy = _build_sandbox_policy(spec, exp_dir)
    coordinator_secret: bytes | None = None
    if spec.enable_peer_mail:
        coordinator_secret = secrets.token_bytes(32)
        logger.info("Peer mail enabled — coordinator secret generated")
    if spec.memory_read_mode in ("shadow", "read_port"):
        coordinator_secret = coordinator_secret or secrets.token_bytes(32)
        logger.info(
            "Memory mode %s — coordinator callback secret generated",
            spec.memory_read_mode,
        )
    state.coordinator_secret = coordinator_secret

    # 5. 环境侧产物快照（barrier 已就绪、任何回合之前）
    hooks.on_environment_ready(state)

    workers: dict[str, Any] = {}
    state.workers = workers
    coordinator: Any = None
    final_metrics: dict = {
        "finished": False,
        "steps": 0,
        "coverage": 0.0,
        "transport_rate": 0.0,
        "elapsed_seconds": 0.0,
    }

    try:
        # 5.1 env 文件（凭据发现面）——api key 灌入进程环境供 RouterAgent 读取
        if spec.env_file:
            state.env = load_env_file(str(spec.env_file)) or {}
            if "api_key" in state.env:
                os.environ[spec.api_key_env] = state.env["api_key"]

        # 6. coordinator 先启动（workers 立即连它的 WS）
        coordinator_factory = spec.coordinator_factory or OrchestratorCoordinator
        coordinator = coordinator_factory(
            env_pack=env_pack,
            host=spec.coordinator_host,
            port=spec.coordinator_port,
            a2a_port=spec.coordinator_port + 1,
            barrier=barrier,
            model=spec.model,
            provider=spec.provider,
            api_base=spec.api_base,
            api_key_env=spec.api_key_env,
            log_dir=str(exp_dir / "coordinator"),
            supervision_dir=str(exp_dir / "supervision"),
            orchestration_mode=spec.orchestration_mode,
            exp_logger=exp_logger,
            sandbox_policy=state.sandbox_policy,
            state_mode=spec.state_mode,
            max_steps=max_steps,
            enable_peer_mail=spec.enable_peer_mail,
            coordinator_secret=coordinator_secret,
            memory_read_mode=spec.memory_read_mode,
            prune_policy=spec.prune_policy,
            run_id=run_id,
            long_term_mode=spec.long_term_mode,
            **hooks.coordinator_kwargs(state),
        )
        state.coordinator = coordinator

        logger.info("Coordinator starting on port %d", spec.coordinator_port)
        state.coord_task = asyncio.create_task(coordinator.start())
        await asyncio.sleep(spec.coordinator_settle_seconds)

        # 6.1 运行期接线（环境/特性侧；SAR：语义图 jsonl 重定向 + 长期记忆/诊断）
        hooks.on_coordinator_started(state)

        # 7. workers（各自线程内起 A2A 服务并连 coordinator）
        worker_factory = spec.worker_factory or OrchestratorWorker
        for i, name in enumerate(spec.agent_names):
            port = spec.agent_base_port + i
            worker = worker_factory(
                env_pack=env_pack,
                worker_id=name,
                agent_name=name,
                agent_idx=i,
                barrier=barrier,
                a2a_host=spec.agent_host,
                a2a_port=port,
                coordinator_url=f"ws://localhost:{spec.coordinator_port}",
                model=spec.model,
                provider=spec.provider,
                api_base=spec.api_base,
                api_key_env=spec.api_key_env,
                log_dir=worker_log_dirs[name],
                exp_logger=exp_logger,
                sandbox_policy=state.sandbox_policy,
                enable_peer_mail=spec.enable_peer_mail,
                coordinator_secret=coordinator_secret,
                memory_read_mode=spec.memory_read_mode,
                prune_policy=spec.prune_policy,
                env_file=spec.env_file,
                **hooks.worker_kwargs(state),
            )
            workers[name] = worker
            worker.start()
            logger.info("Worker %s started on port %d", name, port)

        # 等待 workers 完成向 coordinator 的注册（防 task_not_routable_yet）
        await asyncio.sleep(spec.worker_settle_seconds)

        # 8. 提交初始任务（fire-and-forget —— 并行 poll barrier）
        logger.info("Submitting initial task: %s", spec.task_description)
        a2a_task = asyncio.create_task(coordinator.submit_task(spec.task_description))
        await asyncio.sleep(spec.submit_settle_seconds)

        # 9. poll 循环：步数预算（max_steps）为主、wall-clock 兜底防僵死
        start_time = time.time()
        poll_interval = spec.poll_interval
        _last_step_logged = -1
        a2a_done = False
        a2a_error = False
        coordinator_error = False

        while not barrier.is_finished() and barrier.get_metrics()["steps"] < max_steps:
            await asyncio.sleep(poll_interval)

            if state.coord_task.done():
                exc = state.coord_task.exception()
                if exc:
                    logger.error("Coordinator failed: %s", exc)
                    coordinator_error = True
                    break
            if a2a_task.done() and not a2a_task.cancelled():
                exc = a2a_task.exception()
                if exc:
                    logger.error("A2A orchestration failed: %s", exc)
                    a2a_error = True
                else:
                    result = a2a_task.result()
                    if isinstance(result, str) and result.startswith("Error:"):
                        logger.error("A2A orchestration returned an error: %s", result)
                        a2a_error = True
                    else:
                        logger.info("A2A orchestration completed; exiting poll loop")
                        a2a_done = True
                break

            elapsed = time.time() - start_time
            if elapsed > wall_clock_limit:
                logger.warning(
                    "Wall-clock limit %.0fs reached, stopping (step %d/%d)",
                    wall_clock_limit,
                    barrier.get_metrics()["steps"],
                    max_steps,
                )
                break

            metrics = barrier.get_metrics()
            logger.info(
                "Step %d/%d | Coverage: %.2f | Transport: %.2f | Finished: %s | %.0fs",
                metrics["steps"],
                max_steps,
                metrics["coverage"],
                metrics["transport_rate"],
                metrics["finished"],
                elapsed,
            )

            # 落盘自上次 poll 以来完成的每个回合（不被 poll 间隔丢行）
            drained_logs = (
                barrier.drain_step_logs() if hasattr(barrier, "drain_step_logs") else []
            )
            for step_log in drained_logs:
                step_num = step_log.get("step", metrics["steps"])
                if step_num <= _last_step_logged:
                    continue
                extra = (
                    hooks.step_metrics(state, step_num=step_num, step_log=step_log)
                    or {}
                )
                exp_logger.log_step(
                    step_num=step_num,
                    actions=step_log.get("actions", []),
                    successes=step_log.get("successes", []),
                    observations=step_log.get("observations", []),
                    coverage=step_log.get("coverage", metrics["coverage"]),
                    transport_rate=step_log.get(
                        "transport_rate", metrics["transport_rate"]
                    ),
                    finished=step_log.get("finished", metrics["finished"]),
                    timeout_agents=step_log.get("timeout_agents", []),
                    noop_sources=step_log.get("noop_sources", []),
                    map_recall=extra.get("map_recall", 0.0),
                    freshness=extra.get("freshness", 0.0),
                    run_id=run_id,
                    max_steps=max_steps,
                    remaining_steps=max(0, max_steps - step_num),
                    wall_time_since_start=elapsed,
                    step_duration_ms=step_log.get("step_duration_ms", ""),
                    error_types=step_log.get("error_types", []),
                    completed_subtasks_delta=step_log.get(
                        "completed_subtasks_delta", []
                    ),
                    end_reason="",
                )
                # 增量 summary 写盘（扛 shell timeout kill）
                exp_logger.flush_summary()
                hooks.on_step(
                    state,
                    step_num=step_num,
                    step_log=step_log,
                    drained_logs=drained_logs,
                )
                _last_step_logged = step_num

            # 域级触发器（不受 _last_step_logged 去重影响，与迁移前一致）
            hooks.on_poll(state, metrics=metrics, drained_logs=drained_logs)

        # 10. 终态：先冻结 CSVs（防停机期间在飞回合追加行），再判 end_reason
        if hasattr(exp_logger, "freeze_terminal"):
            exp_logger.freeze_terminal()

        elapsed_total = time.time() - start_time
        # 步数循环可能先于 A2A 请求终态退出——再探一次任务态，避免把失败
        # 请求误判为提前收官。
        if a2a_task.done() and not a2a_task.cancelled():
            a2a_exception = a2a_task.exception()
            if a2a_exception is not None:
                a2a_error = True
            else:
                result = a2a_task.result()
                if isinstance(result, str) and result.startswith("Error:"):
                    a2a_error = True
                else:
                    a2a_done = True
        if state.coord_task.done() and not state.coord_task.cancelled():
            coordinator_exception = state.coord_task.exception()
            if coordinator_exception is not None:
                coordinator_error = True
        final_metrics = barrier.get_metrics()
        final_metrics["elapsed_seconds"] = elapsed_total

        end_reason = classify_end_reason(
            finished=final_metrics["finished"],
            steps=final_metrics["steps"],
            max_steps=max_steps,
            elapsed_seconds=elapsed_total,
            wall_clock_limit=wall_clock_limit,
            a2a_done=a2a_done,
            a2a_error=a2a_error,
            coordinator_error=coordinator_error,
        )
        final_metrics["end_reason"] = end_reason
        exp_logger.set_end_reason(end_reason)
        final_metrics["run_id"] = run_id
        final_metrics["max_steps"] = max_steps

        # 10.1 终态产物冻结（环境侧；SAR：truth recorder drain + manifest）
        final_metrics.update(
            hooks.finalize_artifacts(
                state, final_metrics=final_metrics, end_reason=end_reason
            )
            or {}
        )

        if barrier.is_finished():
            logger.info(
                "TASK COMPLETED in %.1f seconds, %d/%d steps",
                elapsed_total,
                final_metrics["steps"],
                max_steps,
            )
        else:
            logger.warning(
                "TASK TIMEOUT after %d steps (limit %d), %.1f seconds",
                final_metrics["steps"],
                max_steps,
                elapsed_total,
            )

        # 10.2 取消仍然在飞的 A2A 孤儿任务
        if not a2a_task.done():
            a2a_task.cancel()
            try:
                await a2a_task
            except asyncio.CancelledError:
                pass

        # 10.3 run_metrics.json 先落盘（终态评测器读非空 coverage/transport_rate）
        metrics_file = exp_dir / "run_metrics.json"
        try:
            metrics_file.write_text(
                json.dumps(final_metrics, indent=2, default=str), encoding="utf-8"
            )
        except OSError:
            pass

        # 10.4 终态评测（特性侧；SAR：memory acceptance + 长期记忆反思）
        final_metrics.update(
            hooks.run_terminal_evaluations(state, final_metrics=final_metrics) or {}
        )

        return final_metrics

    finally:
        # 11. 收尾：每一步独立防抖——任一环失败也必须完成后续清理。
        logger.info("Shutting down barrier...")
        try:
            barrier.stop()
        except Exception:
            logger.exception("barrier.stop() failed during teardown")

        logger.info("Shutting down workers...")
        for worker in workers.values():
            try:
                worker.stop()
            except Exception:
                logger.exception("worker.stop() failed during teardown")
        if coordinator is not None:
            logger.info("Shutting down coordinator...")
            try:
                await coordinator.stop()
            except Exception:
                logger.exception("coordinator.stop() failed during teardown")

        try:
            logger.info("Clearing agent sessions...")
            for worker in workers.values():
                worker.clear_sessions()
            if coordinator is not None:
                coordinator.clear_sessions()
        except Exception:
            logger.exception("agent session clearing failed during teardown")

        try:
            exp_logger.close()
        except Exception:
            logger.exception("exp_logger.close() failed during teardown")
        try:
            final_metrics["log_dir"] = exp_logger.get_log_dir()
            logger.info("Experiment logs saved to: %s", exp_logger.get_log_dir())
        except Exception:
            logger.exception("final metrics log_dir backfill failed during teardown")
        logger.info("Cleanup complete")
