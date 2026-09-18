"""AI2Thor 实验薄壳（env-contract P5-3）——通用装配骨架的环境侧绑定。

迁移前 ``AI2ThorExperiment``（自建 runner：controller/barrier/round loop/CSV）
已由 ``orchestration.assembly``（P4-4 通用装配）+ 本包环境侧件接管：

- :class:`ai2thor_orch.env_pack.Ai2ThorEnvPack`：barrier 工厂 / worker 工具注册表 /
  coordinator finish_task 工具 / state provider / session 工厂 / prompts（P5-2）；
- :class:`ai2thor_orch.assembly_hooks.AI2ThorAssemblyHooks`：task_config 快照 /
  verifier_trace / 终局 summary.json（P5-3）；
- :class:`ai2thor_orch.logger.AI2ThorExperimentLogger`：metadata / trajectory /
  interactions / token 日志（P5-3）；
- 本模块 ``run_experiment()``：把以上件组装成 ``AssemblySpec`` 并调 ``run_assembly``。

CLI 入口在 :mod:`ai2thor_orch.experiment.__main__`：

    python -m ai2thor_orch.experiment --task 3_transport_groceries \\
        --scene FloorPlan1 --agents 2 --seed 42 --mode fake

运行模式：
- ``fake``（默认）：``FakeController`` 确定性实现，不依赖 ``ai2thor`` 包真运行；
- ``unity``：真实 ``ai2thor.controller.Controller``（P5-4 接线：
  ``ai2thor_orch.executor.unity_controller.UnityController``，``agentCount``
  多 agent 初始化 + 动作映射 + 事件归一化），需 GPU / Unity 主机（远程 A100），
  启动配置见 ``docs/system_docs/ai2thor_a100_runbook.md``。
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from ai2thor_orch.assembly_hooks import AI2ThorAssemblyHooks
from ai2thor_orch.contracts.task import TaskContract, load_task
from ai2thor_orch.env_pack import Ai2ThorEnvPack
from ai2thor_orch.logger import AI2ThorExperimentLogger
from ai2thor_orch.vision_hooks import resolve_vision_config
from orchestration.assembly import AssemblySpec, run_assembly

logger = logging.getLogger("ai2thor_experiment")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_LOGS_ROOT = _PROJECT_ROOT / "logs"

#: 初始任务陈述的使命句（coordinator 的第一条 user 消息；机制细节由 system prompt 承载）。
#: 完整任务陈述由 _default_task_text() 补齐清单/排除/完成条件，不硬编码物品名。
_DEFAULT_TASK_MISSION = (
    "Mission: transport all groceries into the Fridge. "
    "Coordinate your workers to locate each grocery item, pick it up, "
    "and place it inside the Fridge."
)

#: 不进入杂货清单的容器物品（任务目标是「搬进 Fridge」，不是搬运 Fridge）。
_MISSION_CONTAINERS = frozenset({"fridge"})


def _default_task_text(contract: TaskContract) -> str:
    """Build the coordinator's opening mission statement from the task contract.

    清单（``The groceries are exactly these N items: ...``）、排除句（清单外物品一律
    不搬，尤其冰箱旁高频可见的 Egg）与完成条件都从 contract 派生：``coverage_objects``
    就是 checker 判定的目标物品集合；顺序原样保留，容器（``Fridge``）不进入清单。
    这样 coordinator 既不必、也不应从观测里反推清单。coverage 里除容器外没有任何物品时
    退化为仅使命句（不渲染空清单）。
    """
    groceries = [
        name
        for name in contract.coverage_objects
        if name.strip().lower() not in _MISSION_CONTAINERS
    ]
    sentences = [_DEFAULT_TASK_MISSION]
    if groceries:
        sentences.append(
            f"The groceries are exactly these {len(groceries)} items: "
            f"{', '.join(groceries)} (one instance each)."
        )
        sentences.append(
            "Objects of any other type (including any Egg) are NOT part of the "
            "mission — never pick them up or deliver them."
        )
        sentences.append(
            "The mission is complete only when every listed grocery is inside "
            "the Fridge."
        )
    return " ".join(sentences)


def _get_git_commit() -> str:
    """Best-effort HEAD sha for run metadata（读取失败返回空串）。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            cwd=str(_PROJECT_ROOT),
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001 - metadata is best-effort
        return ""


def build_run_metadata(
    *,
    run_id: str,
    task_id: str,
    scene: str,
    num_agents: int,
    agent_names: list[str],
    seed: int,
    mode: str,
    max_steps: int,
    wall_clock_limit: float,
    step_timeout: float,
    model: str,
    provider: str,
    api_base: str,
    contract: TaskContract,
    spawn_mode: str = "default",
    spawn_seed: int | None = None,
    vision_enabled: bool = False,
    vision_model: str | None = None,
    frame_resolution: list[int] | None = None,
) -> dict:
    """组装 ``metadata.json`` 载荷（run 可复现信息，写于任何回合之前）。

    ``seed`` 与 ``spawn_seed`` 语义分列（设计 §3.2）：``seed`` = LLM 采样
    seed；``spawn_seed`` = 物体布局 seed（仅 ``spawn_mode="random"`` 时参与
    ``InitialRandomSpawn``；缺省解析 = run seed）。旧 run 无这两个字段时，
    replay/聚合按 ``spawn_mode="default"`` 解释（历史数据同为默认布局）。

    F-vlm：``vision_enabled`` / ``vision_model`` / ``frame_resolution`` 记录
    本次 run 的视觉注入口径（``vision_hooks.resolve_vision_config``；未激活
    时后二者为 ``None``——字段恒在，schema 稳定）。
    """
    return {
        "run_id": run_id,
        "task_id": task_id,
        "scene": scene,
        "num_agents": num_agents,
        "agent_names": agent_names,
        "seed": seed,
        "mode": mode,
        "spawn_mode": spawn_mode,
        "spawn_seed": spawn_seed,
        # F-vlm：视觉注入口径（未激活时 vision_model / frame_resolution 为 None）。
        "vision_enabled": vision_enabled,
        "vision_model": vision_model,
        "frame_resolution": frame_resolution,
        "max_steps": max_steps,
        "wall_clock_limit": wall_clock_limit,
        "step_timeout": step_timeout,
        "model": model,
        "provider": provider,
        "api_base": api_base,
        "state_mode": "semantic",
        "start_time": datetime.now().isoformat(),  # noqa: DTZ005 - local wall clock
        "code_commit": _get_git_commit(),
        "subtasks": list(contract.subtasks),
        "coverage_objects": list(contract.coverage_objects),
    }


async def run_experiment(
    task_id: str = "3_transport_groceries",
    scene: str = "FloorPlan1",
    num_agents: int = 2,
    seed: int = 42,
    mode: str = "fake",
    max_steps: int = 50,
    model: str | None = None,
    provider: str | None = None,
    api_base: str | None = None,
    api_key_env: str = "OPENAI_API_KEY",
    log_dir: str | None = None,
    coordinator_port: int = 8080,
    agent_base_port: int = 8191,
    wall_clock_limit: float = 3600.0,
    step_timeout: float = 60.0,
    coordinator_prompt: str | None = None,
    spawn_mode: str = "default",
    spawn_seed: int | None = None,
) -> dict:
    """按通用装配骨架跑一次 AI2Thor 实验（薄壳：只做环境侧绑定）。

    环境侧输入经两处注入通用装配：
    - ``Ai2ThorEnvPack(task_id, scene, mode, step_timeout)`` — 契约/工厂；
    - ``AI2ThorAssemblyHooks`` — task_config / verifier_trace / summary.json。

    装配期（barrier → 目录 → logger → coordinator → workers → 初始任务 →
    poll → 终局 → 收尾）全部在 ``orchestration.assembly.run_assembly``。

    Args:
        task_id: ``AI2Thor/Tasks/<task_id>``；当前 verifier 只支持
            ``3_transport_groceries``（fail-fast）。
        scene: FloorPlan 名（如 ``FloorPlan1``）。
        max_steps: 步数预算（barrier 闸门 + poll 循环同口径）。
        model/provider/api_base: LLM 端点（``None`` 时读仓库根 ``.env``）。
        log_dir: 显式运行目录；``None`` 时自动 ``logs/<ts>_<task>_<scene>_...``。
        spawn_mode: 初始布局模式（F-seed）——``"default"``（缺省，与论文
            baseline 同布局）/ ``"random"``（``InitialRandomSpawn`` 随机化，
            失败响亮抛）。
        spawn_seed: 布局 seed（缺省 = ``seed``；seed=LLM 采样 vs
            spawn_seed=布局，语义分列写进 metadata）。run 终结后把共享
            ``AliasRegistry`` 落盘 ``<run_dir>/alias_registry.json``
            （F-frame replay 的 R3 前提）。
        coordinator_prompt: 覆盖初始任务陈述（默认由 ``_default_task_text(contract)``
            从 contract.coverage_objects 派生）。

    F-frame：``LLAMAR_AI2THOR_FRAMES=1``（环境变量）时经 env_pack 把
    ``run_dir`` / ``agent_names`` 接线到帧存储（``<run_dir>/frames/<AgentName>/``，
    捕获点在 UnityController；默认关 = 零副作用）。

    Returns:
        ``run_assembly`` 终态 metrics（含 ``verified_completion`` /
        ``end_reason`` / ``steps`` / token 汇总等）。
    """
    agent_names = ["Alice", "Bob", "Charlie", "David", "Emma", "Finn"][:num_agents]
    contract = load_task(task_id, scene)

    # F-seed：布局 seed 缺省解析（= run seed）在此完成，下游（env_pack /
    # hooks / metadata / controller）只消费已解析值。
    if spawn_seed is None:
        spawn_seed = seed

    if log_dir is None:
        # 本地墙钟（目录名 / metadata 起始时间沿用 SAR 的本地时间戳约定）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")  # noqa: DTZ005
        log_dir = str(
            _LOGS_ROOT
            / f"{timestamp}_{task_id}_{scene}_a{num_agents}_seed{seed}_{mode}"
        )

    run_id = f"ai2thor-{task_id}-agents{num_agents}-seed{seed}-{uuid.uuid4().hex[:8]}"

    env_pack = Ai2ThorEnvPack(
        task_id=task_id,
        scene=scene,
        mode=mode,
        step_timeout=step_timeout,
        spawn_mode=spawn_mode,
        spawn_seed=spawn_seed,
        # P2 空间记忆：sightings store 的落盘目录（<run_dir>/sightings.ndjson）。
        run_dir=log_dir,
        # F-frame：帧目录名映射（<run_dir>/frames/<AgentName>/，LLAMAR_AI2THOR_FRAMES
        # 开关置位时经 build_barrier 接线到 UnityController）。
        agent_names=agent_names,
    )
    hooks = AI2ThorAssemblyHooks(
        task_id=task_id,
        scene=scene,
        mode=mode,
        seed=seed,
        num_agents=num_agents,
        contract=contract,
        spawn_mode=spawn_mode,
        spawn_seed=spawn_seed,
    )

    # P5-3：LLM 端点走 .env（显式参数优先）——与 SAR 薄壳同一发现面。
    from a2a.shared.env_loader import load_env_file

    env = load_env_file(str(_PROJECT_ROOT / ".env")) or {}
    model = model or env.get("model", "deepseek-v4-flash")
    provider = provider or env.get("provider", "openai")
    api_base = api_base or env.get("api_base", "https://api.deepseek.com")

    # F-vlm：视觉注入口径（VLM=1 ∧ FRAMES=1 ∧ mode=unity 才激活；缺配时
    # 装配期有响亮警告——metadata 记录实际生效口径供 sweep 过滤）。
    vision_config = resolve_vision_config(mode=mode, model=model)

    metadata = build_run_metadata(
        run_id=run_id,
        task_id=task_id,
        scene=scene,
        num_agents=num_agents,
        agent_names=agent_names,
        seed=seed,
        mode=mode,
        max_steps=max_steps,
        wall_clock_limit=wall_clock_limit,
        step_timeout=step_timeout,
        model=model,
        provider=provider,
        api_base=api_base,
        contract=contract,
        spawn_mode=spawn_mode,
        spawn_seed=spawn_seed,
        vision_enabled=vision_config["vision_enabled"],
        vision_model=vision_config["vision_model"],
        frame_resolution=vision_config["frame_resolution"],
    )

    spec = AssemblySpec(
        env_pack=env_pack,
        log_dir=log_dir,
        num_agents=num_agents,
        agent_names=agent_names,
        seed=seed,
        run_id=run_id,
        model=model,
        provider=provider,
        api_base=api_base,
        api_key_env=api_key_env,
        max_steps=max_steps,
        wall_clock_limit=wall_clock_limit,
        coordinator_port=coordinator_port,
        agent_base_port=agent_base_port,
        state_mode="semantic",
        sandbox_profile="workspace",
        project_root=str(_PROJECT_ROOT),
        # P5-3 范围：不启用 canonical Memory / long-term / peer-mail 通道
        # （SAR H3 特有面；AI2Thor 接线如需再单独立项）。
        memory_read_mode="legacy",
        long_term_mode="off",
        task_description=coordinator_prompt or _default_task_text(contract),
        # barrier 工厂消费：max_steps 必填（与 run 预算同口径）、其余覆盖缺省。
        env_params={
            "scene": scene,
            "mode": mode,
            "step_timeout": step_timeout,
            "max_steps": max_steps,
        },
        metadata=metadata,
        env_file=str(_PROJECT_ROOT / ".env"),
        logger_factory=lambda d: AI2ThorExperimentLogger(
            experiment_name=task_id, log_dir=d
        ),
        hooks=hooks,
    )
    try:
        return await run_assembly(spec)
    finally:
        # F-seed：run 终结（正常或异常路径）把共享 AliasRegistry 落盘
        # ``<run_dir>/alias_registry.json``（F-frame replay 的 R3 前提）。
        # 收尾路径不允许再抛——失败仅记录，不掩盖 run_assembly 的异常。
        _dump_alias_registry(env_pack, log_dir)


def _dump_alias_registry(env_pack: Ai2ThorEnvPack, log_dir: str) -> None:
    """run 终结时把 ``barrier.alias_registry`` 落盘到 ``<run_dir>/alias_registry.json``。

    共享实例 = ``barrier.alias_registry``（worker 工具 / 观测脱敏的同一注册表）；
    barrier 未构建（run 早期失败）时跳过。落盘失败仅记录——收尾阶段不允许
    再抛异常（与 ``run_assembly`` teardown 同一约定）。
    """
    barrier = env_pack.barrier
    registry = getattr(barrier, "alias_registry", None)
    if registry is None:
        return
    try:
        path = registry.dump(Path(log_dir) / "alias_registry.json")
        logger.info(
            "alias_registry.json dumped: %s (%d entries)", path, registry.size
        )
    except Exception:
        # 收尾路径不允许再抛（仅记录）——与 run_assembly teardown 同一约定。
        logger.exception("alias_registry.json 落盘失败（已忽略）")


__all__ = ["build_run_metadata", "run_experiment"]
