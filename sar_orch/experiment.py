#!/usr/bin/env python3
"""SAR 端到端实验运行器（装配薄壳；env-contract P4-4）。

P4-4 起本模块只保留 SAR 特化面：

- CLI 参数面（``main``）与运行目录命名约定（``results/{ts}_s{scene}_s{seed}_a{n}``）；
- run metadata 构造（``build_run_metadata``）与 prompts 默认解析；
- 终局判定 re-export（``classify_end_reason``，实现迁至通用装配层）；
- ``SAREnvPack`` 构造（prompts / map_summary 路径经 EnvPack 消费，P4-1 面）
  与 ``SARAssemblyHooks`` 接线。

通用装配/运维骨架（barrier 工厂消费、coordinator/worker 服务启动、线程、
poll 循环、wall-clock 兜底、收尾、端口选择、日志接线）在
``orchestration.assembly.run_assembly``；SAR 装配期特化件（scene_config 快照 /
truth recorder / 长期记忆·诊断接线 / 终态评测）在 ``sar_orch.assembly_hooks``。

产物与 CLI 参数面与 P4-4 前零变化（run 目录布局、CSV/NDJSON/metadata 字段、
端到端行为逐段对应）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from a2a.shared.env_loader import load_env_file
from orchestration.assembly import (
    AssemblySpec,
    classify_end_reason,
    run_assembly,
)
from sar_orch.assembly_hooks import (
    SARAssemblyHooks,
)
from sar_orch.assembly_hooks import (
    # 兼容 re-export：既有测试/调用方从 ``sar_orch.experiment`` 导入该符号；
    # 显式 ``as`` 别名标记「有意重导出」（避免 F401 误报）。
    _invoke_run_terminal_long_term_reflection as _invoke_run_terminal_long_term_reflection,  # noqa: PLC0414
)
from sar_orch.env_pack import SAREnvPack
from sar_orch.logger import ExperimentLogger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("sar_experiment")

# Port assignments for up to 6 agents
AGENT_PORTS: dict[str, int] = {
    "Alice": 8191,
    "Bob": 8192,
    "Charlie": 8193,
    "David": 8194,
    "Emma": 8195,
    "Finn": 8196,
}

COORDINATOR_PORT = 8080

#: LLaMAR 论文 §5 的规划视界上限 L。论文原文："Average steps (L): The number
#: of high-level actions taken by the team to complete the task, capped at
#: L = 30 in our experiments. If the task is not completed within L steps, the
#: episode is deemed a failure."
#: 与论文表格对照时必须用这个值，否则 SR/TR/C/L 全都不可比。
PAPER_MAX_STEPS = 30

# Paths
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_COORDINATOR_PROMPTS = os.path.join(_PROJECT_ROOT, "sar_orch", "prompts", "coordinator")
_WORKER_PROMPTS = os.path.join(_PROJECT_ROOT, "sar_orch", "prompts", "worker")

# Default results root (all experiment outputs go under sar_orch/results/)
_RESULTS_ROOT = os.path.join(_PROJECT_ROOT, "sar_orch", "results")

#: 默认初始任务（未传 --coordinator-prompt 时）。
_DEFAULT_TASK = "Extinguish all fires and rescue all persons"

__all__ = [
    "PAPER_MAX_STEPS",
    "build_run_metadata",
    "classify_end_reason",
    "main",
    "run_experiment",
]


def _get_git_commit() -> str:
    """Best-effort read of the current git short SHA."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _sha256_file_fingerprint(path: str) -> str:
    """Best-effort sha256 content fingerprint (hex, first 12 chars) of a file.

    Returns "" when the file is missing or unreadable so callers can build
    metadata without crashing on optional prompt layouts.
    """
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return ""


def _resolve_coordinator_prompt_file(prompts_dir: str, state_mode: str) -> str:
    """Return the coordinator prompt file actually loaded for the given mode.

    Mirrors sar_orch/coordinator.py: semantic mode overrides the router's
    default system.md with system.semantic.md when it exists; all other modes
    (baseline/oracle) use system.md.
    """
    if state_mode == "semantic":
        semantic_path = os.path.join(prompts_dir, "system.semantic.md")
        if os.path.exists(semantic_path):
            return semantic_path
    return os.path.join(prompts_dir, "system.md")


def _default_truth_dir(exp_dir_name: str, run_id: str) -> Path:
    """Default evaluator-private truth output dir for a run.

    Naming rule: ``<log-dir-basename>-<uuid8>`` — the run_id tail
    (``run_id.rsplit('-', 1)[-1]``, e.g. ``099a58c3`` of
    ``sar-scene3-agents4-seed0-099a58c3``) makes the directory unique per run
    even when explicit ``--log-dir`` values share the same basename (driver
    ``agents_N/seed_M`` or benchmark ``scene_S/agents_A/seed_N`` both collapse
    to ``seed_N``).  Auto-generated run names already embed a timestamp in the
    basename; appending the uuid8 keeps every truth dir unique unconditionally.
    """
    return Path(_RESULTS_ROOT) / "truth" / f"{exp_dir_name}-{run_id.rsplit('-', 1)[-1]}"


def _make_experiment_logger(log_dir: str) -> ExperimentLogger:
    """SAR 实验日志对象工厂（装配层经 ``logger_factory`` 注入）。

    保留为模块级函数：``ExperimentLogger`` 在调用期从本模块全局解析，既有
    测试仍可 monkeypatch ``sar_orch.experiment.ExperimentLogger``。
    """
    return ExperimentLogger(experiment_name="sar_experiment", log_dir=log_dir)


def build_run_metadata(
    *,
    run_id: str,
    scene: int,
    num_agents: int,
    seed: int,
    model: str,
    provider: str,
    api_base: str,
    max_steps: int,
    wall_clock_limit: float,
    sandbox_profile: str,
    coordinator_prompts: str,
    worker_prompts: str,
    code_commit: str = "",
    state_mode: str = "semantic",
    long_term_mode: str = "off",
    memory_read_mode: str = "read_port",
    prune_policy: str = "count_window",
) -> dict:
    return {
        "run_id": run_id,
        "env_name": "SAR",
        "scenario_id": f"scene_{scene}",
        "scene": scene,
        "seed": seed,
        "agent_count": num_agents,
        "model": model,
        "provider": provider,
        "api_base": api_base,
        "max_steps": max_steps,
        "wall_clock_timeout": wall_clock_limit,
        "sandbox_profile": sandbox_profile,
        "task_objective": "Extinguish all fires and rescue all persons",
        "success_criteria": "SAR checker subtasks complete",
        "coordinator_prompts": coordinator_prompts,
        "worker_prompts": worker_prompts,
        "prompt_version": "baseline",
        "worker_prompt_sha256": _sha256_file_fingerprint(
            os.path.join(worker_prompts, "system.md")
        ),
        "coordinator_prompt_sha256": _sha256_file_fingerprint(
            _resolve_coordinator_prompt_file(coordinator_prompts, state_mode)
        ),
        "code_commit": code_commit or _get_git_commit(),
        "long_term_mode": long_term_mode,
        "memory_read_mode": memory_read_mode,
        "prune_policy": prune_policy,
    }


def _load_long_term_runtime_config(long_term_mode: str):
    """加载 long_term.config（长期记忆 + 诊断 tunables）。

    D9：不可解析/非法的配置文件强制 ``long_term_mode=off``（typed 告警，
    绝不静默进入运行中的触发器）；``[diagnosis]`` 段非法只关诊断通道
    （fail-closed，D8 非必需通道，不影响长期记忆）。

    Returns:
        ``(effective_mode, lt_config, diag_runtime)``。
    """
    lt_config = None
    diag_runtime = None
    config_path = str(Path(__file__).parent.parent / "long_term.config")
    if long_term_mode == "off":
        return long_term_mode, lt_config, diag_runtime
    try:
        from sar_orch.long_term_reflection import load_long_term_config

        lt_config = load_long_term_config(config_path)
    except Exception as exc:  # noqa: BLE001 - LongTermConfigError -> off
        logger.warning(
            "long_term.config invalid — forcing long_term_mode=off: %s", exc
        )
        return "off", None, None
    try:
        from sar_orch.long_term_reflection import load_diagnosis_config

        diag_runtime = load_diagnosis_config(config_path)
    except Exception as exc:  # noqa: BLE001 - DiagnosisConfigError -> off
        logger.warning(
            "long_term.config [diagnosis] invalid — diagnosis channel "
            "disabled: %s",
            exc,
        )
        diag_runtime = None
    return long_term_mode, lt_config, diag_runtime


async def run_experiment(
    scene: int = 1,
    num_agents: int = 2,
    seed: int = 42,
    model: str = "deepseek-v4-flash",
    provider: str = "openai",
    api_base: str = "https://api.deepseek.com",
    api_key_env: str = "OPENAI_API_KEY",
    max_steps: int | None = None,
    coordinator_port: int = 8080,
    agent_base_port: int = 8191,
    log_dir: str | None = None,
    sandbox_profile: str = "workspace",
    state_mode: str = "semantic",
    coordinator_prompt: str | None = None,
    enable_peer_mail: bool = False,
    memory_read_mode: str = "read_port",
    truth_manifest: str | None = None,
    truth_trace: str | None = None,
    truth_output_dir: str | None = None,
    # Phase 4: run-local long-term memory mode (off|shadow|read).  ``off``
    # performs zero long-term DB I/O; shadow/read persist published
    # reflection products (atomic committed snapshots) but never inject
    # them into Context (P5).  Phase 4 never invokes a real model.
    long_term_mode: str = "off",
    # P1 cache optimization: worker/coordinator history pruning policy
    # (count_window | prefix_stable).  Default keeps legacy behavior; the
    # armed value is recorded in metadata.json as ``prune_policy``.
    prune_policy: str = "count_window",
    # G6 (env-contract): injectable prompts roots for the SAR assembly.
    # ``None`` keeps the current in-tree layout
    # (sar_orch/prompts/{coordinator,worker}) byte-for-byte unchanged; an
    # explicit directory is passed through to the SAR EnvPack (P4-4: consumed
    # by the generic assembly via EnvPack, fail-fast when missing).
    coordinator_prompts_dir: str | None = None,
    worker_prompts_dir: str | None = None,
) -> dict:
    """Run one full SAR experiment.

    Args:
        max_steps: Maximum environment steps (step-based, like original LLaMAR).
            Defaults to ``PAPER_MAX_STEPS`` (30) — the LLaMAR paper §5 planning
            horizon ``L``; the per-scene ``task_timeout`` is intentionally NOT
            used (scene_1=1200 vs scene_2-5=35 would make scenes incomparable).
        coordinator_port: Port for the coordinator HTTP server.
        agent_base_port: Base port for agent A2A servers (each agent gets base + index).
        log_dir: Explicit log directory. If None, auto-generated timestamp dir.
        coordinator_prompt: Optional override for the initial task sent to the coordinator.
        truth_manifest: Evaluator-private truth manifest (JSON) frozen after run
            terminal; when provided the terminal-only memory_projection_quality
            evaluator runs (canonical Memory mode only).
        truth_trace: Optional override of the truth trace path in the manifest.
        truth_output_dir: Optional override of the default evaluator-private
            directory for the Phase 5 truth recorder (per-step
            truth_trace.jsonl + terminal truth_manifest.json).  The recorder
            is enabled by default at
            ``sar_orch/results/truth/<log-dir-basename>-<uuid8>/`` — the uuid8
            (run_id tail) makes the directory unique per run even when
            explicit ``--log-dir`` values share a basename (outside the run
            results dir — H1 evaluator-private boundary: the recorder never
            writes anywhere agents can read); pass an explicit directory here
            to relocate it.  When no explicit
            ``truth_manifest`` is given, the generated manifest is wired into
            the terminal memory_projection_quality evaluator.
        coordinator_prompts_dir: Optional override of the coordinator prompts
            root (default: the in-tree ``sar_orch/prompts/coordinator``).
        worker_prompts_dir: Optional override of the worker prompts root
            (default: the in-tree ``sar_orch/prompts/worker``).
    """
    # G6 (env-contract): prompts roots are an explicit, injectable assembly
    # parameter.  ``None`` resolves to the current in-tree layout, so the
    # default behavior stays byte-for-byte identical.
    coordinator_prompts_dir = coordinator_prompts_dir or _COORDINATOR_PROMPTS
    worker_prompts_dir = worker_prompts_dir or _WORKER_PROMPTS

    agent_names = ["Alice", "Bob", "Charlie", "David", "Emma", "Finn"][:num_agents]

    logger.info("=" * 60)
    logger.info("SAR Experiment: scene=%d, agents=%d, seed=%d", scene, num_agents, seed)
    logger.info("Model: %s (provider=%s)", model, provider)
    logger.info("=" * 60)

    # 论文 §5 把规划视界固定为 L=30（"capped at L=30 in our experiments.
    # If the task is not completed within L steps, the episode is deemed a
    # failure."）。场景自带的 task_timeout 并不统一（scene_1 是 1200，
    # scene_2–5 是 35），直接用它会让不同场景跑在不可比的预算下，且与论文
    # 报告的 L 口径不一致。因此默认取 PAPER_MAX_STEPS，显式传 --max-steps
    # 才覆盖。
    max_steps = max_steps or PAPER_MAX_STEPS

    # 2. 运行目录（统一命名约定：results/{ts}_s{scene}_s{seed}_a{agents}）
    if log_dir is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        experiment_dir_name = f"{timestamp}_s{scene}_s{seed}_a{num_agents}"
        log_dir = str(Path(_RESULTS_ROOT) / experiment_dir_name)
    exp_dir = Path(log_dir)

    run_id = f"sar-scene{scene}-agents{num_agents}-seed{seed}-{uuid.uuid4().hex[:8]}"
    wall_clock_limit = 3600.0

    # Phase 5: evaluator-private truth recorder（默认开启）。输出目录必须在
    # run results 之外（H1 边界：agents/workers 永不可读原始 truth trace）；
    # 显式 --truth-output-dir 覆盖默认
    # ``sar_orch/results/truth/<log-dir-basename>-<uuid8>/``（uuid8 = run_id
    # 尾段；同名 --log-dir 取值下也唯一，见 _default_truth_dir）。
    if truth_output_dir is not None:
        truth_out = Path(truth_output_dir)
    else:
        truth_out = _default_truth_dir(exp_dir.name, run_id)
    try:
        if str(truth_out.resolve()).startswith(str(exp_dir.resolve())):
            raise ValueError(
                f"--truth-output-dir {truth_out} must not be inside the run "
                f"results dir {exp_dir} (evaluator-private boundary)"
            )
    except OSError:
        pass  # resolution edge cases fall through to recorder init

    # Phase 4: long_term.config tunables（D9；非法配置强制 off，绝不静默）
    long_term_mode, lt_config, diag_runtime = _load_long_term_runtime_config(
        long_term_mode
    )

    code_commit = _get_git_commit()
    metadata = build_run_metadata(
        run_id=run_id,
        code_commit=code_commit,
        scene=scene,
        num_agents=num_agents,
        seed=seed,
        model=model,
        provider=provider,
        api_base=api_base,
        max_steps=max_steps,
        wall_clock_limit=wall_clock_limit,
        sandbox_profile=sandbox_profile,
        coordinator_prompts=coordinator_prompts_dir,
        worker_prompts=worker_prompts_dir,
        state_mode=state_mode,
        long_term_mode=long_term_mode,
        memory_read_mode=memory_read_mode,
        prune_policy=prune_policy,
    )
    metadata["state_mode"] = state_mode
    metadata["oracle_mode"] = state_mode == "oracle"
    metadata["enable_peer_mail"] = enable_peer_mail
    metadata["truth_dir"] = str(truth_out)

    # P4-4: prompts / map_summary 路径经 EnvPack 携带（通用装配层从契约面消费；
    # 不再逐参数透传给 coordinator/worker 构造器）。
    env_pack = SAREnvPack(
        coordinator_prompts_dir=coordinator_prompts_dir,
        worker_prompts_dir=worker_prompts_dir,
        map_summary_path=str(exp_dir / "map_summary.jsonl"),
    )
    hooks = SARAssemblyHooks(
        scene=scene,
        seed=seed,
        num_agents=num_agents,
        truth_out=truth_out,
        memory_read_mode=memory_read_mode,
        long_term_mode=long_term_mode,
        lt_config=lt_config,
        diag_runtime=diag_runtime,
        truth_manifest=truth_manifest,
        truth_trace=truth_trace,
    )
    spec = AssemblySpec(
        env_pack=env_pack,
        log_dir=str(exp_dir),
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
        state_mode=state_mode,
        sandbox_profile=sandbox_profile,
        project_root=_PROJECT_ROOT,
        enable_peer_mail=enable_peer_mail,
        memory_read_mode=memory_read_mode,
        long_term_mode=long_term_mode,
        prune_policy=prune_policy,
        task_description=coordinator_prompt or _DEFAULT_TASK,
        env_params={"scene": scene},
        metadata=metadata,
        # 现状 .env 位置：仓库根（P4-3 起由薄壳显式提供凭据发现面）。
        env_file=str(Path(__file__).parent.parent / ".env"),
        logger_factory=_make_experiment_logger,
        hooks=hooks,
    )
    return await run_assembly(spec)


def main():
    parser = argparse.ArgumentParser(description="SAR Experiment")
    parser.add_argument("--scene", type=int, default=1, help="SAR scene number (1-5)")
    parser.add_argument("--agents", type=int, default=2, help="Number of agents (1-6)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")

    # Load .env FIRST so its values become CLI defaults (CLI args still take precedence)
    _env = load_env_file(str(Path(__file__).parent.parent / ".env"))
    parser.add_argument(
        "--model",
        type=str,
        default=_env.get("model", "deepseek-v4-flash"),
        help="LLM model",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=_env.get("provider", "openai"),
        help="LLM provider",
    )
    parser.add_argument(
        "--api-base",
        type=str,
        default=_env.get("api_base", "https://api.deepseek.com"),
        help="API base URL",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help=f"Max environment steps (default: {PAPER_MAX_STEPS}, the paper's L cap)",
    )
    parser.add_argument(
        "--coordinator-port",
        type=int,
        default=8080,
        help="Coordinator server port (default: 8080)",
    )
    parser.add_argument(
        "--agent-base-port",
        type=int,
        default=8191,
        help="Base port for agent A2A servers (default: 8191)",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=None,
        help="Explicit log directory (default: auto-generated timestamp dir)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="semantic",
        choices=["semantic", "oracle"],
        help="Coordinator state source mode (default: semantic)",
    )
    parser.add_argument(
        "--sandbox-profile",
        type=str,
        default="workspace",
        choices=["off", "workspace"],
        help="Sandbox profile: off|workspace (default: workspace)",
    )
    parser.add_argument(
        "--coordinator-prompt",
        type=str,
        default=None,
        help="Override the initial task prompt sent to the coordinator (for targeted testing)",
    )
    parser.add_argument(
        "--enable-peer-mail",
        action="store_true",
        default=False,
        help="Enable signed envelope peer messaging (Phase 4)",
    )
    parser.add_argument(
        "--memory-read-mode",
        type=str,
        default="read_port",
        choices=["legacy", "shadow", "read_port"],
        help="Memory mode: legacy|shadow|read_port (default read_port since "
        "H3 retirement; legacy retained as rollback target)",
    )
    parser.add_argument(
        "--long-term-mode",
        type=str,
        default="off",
        choices=["off", "shadow", "read"],
        help="Run-local long-term memory mode (default off). shadow/read "
        "persist published reflection products from committed snapshots but "
        "never inject them into Context (P5); Phase 4 performs no real model "
        "calls (reflection_* keys in .env gate the model port)",
    )
    parser.add_argument(
        "--prune-policy",
        type=str,
        default="count_window",
        choices=["count_window", "prefix_stable"],
        help="History pruning policy for worker + coordinator contexts "
        "(default count_window = legacy sliding window; prefix_stable = "
        "append-only prefix discipline, P1 cache optimization opt-in). "
        "The effective value is recorded in metadata.json as prune_policy",
    )
    parser.add_argument(
        "--truth-manifest",
        type=str,
        default=None,
        help="Evaluator-private truth manifest path; when provided, the "
        "terminal-only memory_projection_quality evaluator runs after the run "
        "(requires canonical Memory mode shadow/read_port)",
    )
    parser.add_argument(
        "--truth-trace",
        type=str,
        default=None,
        help="Optional override of the truth trace path declared in "
        "--truth-manifest",
    )
    parser.add_argument(
        "--truth-output-dir",
        type=str,
        default=None,
        help="Optional override of the default evaluator-private truth "
        "recorder directory (default: sar_orch/results/truth/"
        "<log-dir-basename>-<uuid8>/). "
        "Per-step truth_trace.jsonl + terminal truth_manifest.json are "
        "written there; the generated manifest is wired into the terminal "
        "memory_projection_quality evaluator unless --truth-manifest is "
        "given. Must be outside the run results dir (evaluator-private "
        "boundary).",
    )
    args = parser.parse_args()

    metrics = asyncio.run(
        run_experiment(
            scene=args.scene,
            num_agents=args.agents,
            seed=args.seed,
            model=args.model,
            provider=args.provider,
            api_base=args.api_base,
            max_steps=args.max_steps,
            coordinator_port=args.coordinator_port,
            agent_base_port=args.agent_base_port,
            log_dir=args.log_dir,
            sandbox_profile=args.sandbox_profile,
            state_mode=args.mode,
            coordinator_prompt=args.coordinator_prompt,
            enable_peer_mail=args.enable_peer_mail,
            memory_read_mode=args.memory_read_mode,
            long_term_mode=args.long_term_mode,
            prune_policy=args.prune_policy,
            truth_manifest=args.truth_manifest,
            truth_trace=args.truth_trace,
            truth_output_dir=args.truth_output_dir,
        )
    )

    # Write metrics to JSON for subprocess caller
    log_dir = metrics.get("log_dir", "")
    if log_dir:
        metrics_file = os.path.join(log_dir, "run_metrics.json")
        with open(metrics_file, "w") as f:
            json.dump(metrics, f, indent=2, default=str)

    print(json.dumps(metrics, default=str))


if __name__ == "__main__":
    main()
