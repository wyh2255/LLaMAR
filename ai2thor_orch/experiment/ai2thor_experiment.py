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
- ``unity``：清晰 stub（``create_controller`` 抛 ``NotImplementedError``，
  真机接线是 P5-4）——fake 路径永不走到这里。
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
from orchestration.assembly import AssemblySpec, run_assembly

logger = logging.getLogger("ai2thor_experiment")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_LOGS_ROOT = _PROJECT_ROOT / "logs"

#: 初始任务陈述（coordinator 的第一条 user 消息；机制细节由 system prompt 承载）。
_DEFAULT_TASK = (
    "Mission: transport all groceries into the Fridge. "
    "Coordinate your workers to locate each grocery item, pick it up, "
    "and place it inside the Fridge."
)


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
) -> dict:
    """组装 ``metadata.json`` 载荷（run 可复现信息，写于任何回合之前）。"""
    return {
        "run_id": run_id,
        "task_id": task_id,
        "scene": scene,
        "num_agents": num_agents,
        "agent_names": agent_names,
        "seed": seed,
        "mode": mode,
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
        coordinator_prompt: 覆盖初始任务陈述（默认 ``_DEFAULT_TASK``）。

    Returns:
        ``run_assembly`` 终态 metrics（含 ``verified_completion`` /
        ``end_reason`` / ``steps`` / token 汇总等）。
    """
    agent_names = ["Alice", "Bob", "Charlie", "David", "Emma", "Finn"][:num_agents]
    contract = load_task(task_id, scene)

    if log_dir is None:
        # 本地墙钟（目录名 / metadata 起始时间沿用 SAR 的本地时间戳约定）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")  # noqa: DTZ005
        log_dir = str(
            _LOGS_ROOT
            / f"{timestamp}_{task_id}_{scene}_a{num_agents}_seed{seed}_{mode}"
        )

    run_id = f"ai2thor-{task_id}-agents{num_agents}-seed{seed}-{uuid.uuid4().hex[:8]}"

    env_pack = Ai2ThorEnvPack(
        task_id=task_id, scene=scene, mode=mode, step_timeout=step_timeout
    )
    hooks = AI2ThorAssemblyHooks(
        task_id=task_id,
        scene=scene,
        mode=mode,
        seed=seed,
        num_agents=num_agents,
        contract=contract,
    )

    # P5-3：LLM 端点走 .env（显式参数优先）——与 SAR 薄壳同一发现面。
    from a2a.shared.env_loader import load_env_file

    env = load_env_file(str(_PROJECT_ROOT / ".env")) or {}
    model = model or env.get("model", "deepseek-v4-flash")
    provider = provider or env.get("provider", "openai")
    api_base = api_base or env.get("api_base", "https://api.deepseek.com")

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
        task_description=coordinator_prompt or _DEFAULT_TASK,
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
    return await run_assembly(spec)


__all__ = ["build_run_metadata", "run_experiment"]
