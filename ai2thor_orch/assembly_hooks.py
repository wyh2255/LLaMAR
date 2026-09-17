"""AI2Thor 装配钩子 —— ``orchestration.assembly.AssemblyHooks`` 的 AI2Thor 实现（P5-3）。

承载从迁移前 ``AI2ThorExperiment`` 迁出的装配期特化件：

- ``on_environment_ready``：任务契约快照 ``task_config.json``（任何回合之前，
  运行目录内稳定的任务配置基线）；
- ``on_step``：逐回合验证轨迹 ``verifier_trace.ndjson``（每个被落盘的 env step
  一行：verifier 判定 + 域指标，供审计与失败定位）；
- ``finalize_artifacts``：终局 ``summary.json``（benchmark 消费面，字段与迁移前
  ``AI2ThorExperiment.run()`` 写出的 v2 schema 对齐并扩展）——verifier 终判
  （postcondition 真值）+ task-metrics 累计指标 + end_reason。

通用装配流程（barrier/coordinator/worker/poll/收尾）在
``orchestration.assembly``；本模块只做 AI2Thor 侧产物动作。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from a2a.coordinator.task_watchdog import WatchdogConfig
from ai2thor_orch.contracts.task import TaskContract
from orchestration.assembly import AssemblyHooks, AssemblyState

logger = logging.getLogger("ai2thor_orch.assembly_hooks")


def build_watchdog_config() -> WatchdogConfig:
    """AI2Thor TaskWatchdog 阈值（C2b 真机校准）。

    证据：A100 首跑报告 §4-4 + ``reports/a100_firstrun_20260914`` 的
    ``l3_short`` / ``l3_full`` supervision 录制。

    - ``stale_requires_both=True``：TASK_STALE 需「步数 + 时间」双条件同时
      满足（框架缺省是「或」）——正常动作（MoveAhead/Pickup 等）不刷新
      progress 是设计语义，单维阈值在真机节奏下必有一侧过急；
    - ``no_progress_step_threshold=10``：旧值 3 在 ~2.6s/回合下 ≈ 8s 即触发
      （A100 短跑实测触发点 steps_since=6 / progress_age=10.0s：2 个 dispatch
      各 1×TASK_STALE+1×RECOVERED，worker 全程正常）；10 回合 ≈ 22s，仅作
      慢节奏场景的步数下限（防「时间单条件」在慢回合下误报）;
    - ``task_stale_seconds=90.0``：A100 实测最长正常无进展窗 ≈ 62s（progress
      只在观测上报 / artifact / 域指标变化时刷新），留 1.4× 余量；低于框架
      缺省 120s，因为 50 步预算的 run 全程仅 ~2-3 分钟。

    反向兜底不变：真停滞（双条件同时超限）仍报 TASK_STALE 并可 recover；
    另有派发级有界兜底 DEADLINE_WARNING(300s) / EXCEEDED(600s)。
    """
    return WatchdogConfig(
        task_stale_seconds=90.0,
        no_progress_step_threshold=10,
        stale_requires_both=True,
    )


class AI2ThorAssemblyHooks(AssemblyHooks):
    """AI2Thor 装配生命周期钩子。

    构造参数由 AI2Thor 装配薄壳（``ai2thor_orch/experiment/ai2thor_experiment.py``）
    提供：任务契约（快照 / 终判输入）与运行标识（产物元数据）。
    """

    def __init__(
        self,
        *,
        task_id: str,
        scene: str,
        mode: str,
        seed: int,
        num_agents: int,
        contract: TaskContract,
        spawn_mode: str = "default",
        spawn_seed: int | None = None,
    ) -> None:
        self._task_id = task_id
        self._scene = scene
        self._mode = mode
        self._seed = seed
        self._num_agents = num_agents
        self._contract = contract
        #: F-seed：初始布局模式与布局 seed（缺省 default = 论文 baseline 同布局；
        #: 「缺省 = run seed」由 ``run_experiment`` 解析后传入——这里是机械透传，
        #: 不重复实现解析规则）。落进 task_config.json 供 replay 重建布局。
        self._spawn_mode = spawn_mode
        self._spawn_seed = spawn_seed

    # ── 装配窗口 ─────────────────────────────────────────────────────────

    def coordinator_kwargs(self, state: AssemblyState) -> dict[str, Any]:
        """AI2Thor coordinator 构造参数：watchdog 真机阈值（C2b）。

        经 ``run_assembly`` 透传 ``OrchestratorCoordinator(watchdog_config=...)``
        → ``create_server`` → ``TaskWatchdog(config=...)``；SAR/缺省路径不传，
        内核缺省值逐字不变。
        """
        return {"watchdog_config": build_watchdog_config()}

    def on_environment_ready(self, state: AssemblyState) -> None:
        """barrier 就绪、任何回合之前：写 ``task_config.json`` 快照。"""
        config = {
            "schema_version": 1,
            "task_id": self._task_id,
            "scene": self._scene,
            "mode": self._mode,
            "seed": self._seed,
            "num_agents": self._num_agents,
            "max_steps": state.max_steps,
            # F-seed：seed（LLM 采样）与 spawn_seed（物体布局）语义分列；
            # replay/聚合对缺字段旧 run 按 spawn_mode="default" 解释。
            "spawn_mode": self._spawn_mode,
            "spawn_seed": self._spawn_seed,
            "subtasks": list(self._contract.subtasks),
            "coverage_objects": list(self._contract.coverage_objects),
            "initial_inventory": {
                str(idx): list(items)
                for idx, items in self._contract.initial_inventory.items()
            },
        }
        out_path = state.exp_dir / "task_config.json"
        out_path.write_text(
            json.dumps(config, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info("Task config snapshot written: %s", out_path)

    def on_step(
        self,
        state: AssemblyState,
        *,
        step_num: int,
        step_log: dict,
        drained_logs: list,
    ) -> None:
        """逐回合验证轨迹：每个落盘回合一行 ``verifier_trace.ndjson``。"""
        trace_path = state.exp_dir / "verifier_trace.ndjson"
        row = {
            "step": step_num,
            "verified_completion": bool(step_log.get("verified_completion", False)),
            "coverage": float(step_log.get("coverage", 0.0)),
            "transport_rate": float(step_log.get("transport_rate", 0.0)),
            "interaction_coverage": float(step_log.get("interaction_coverage", 0.0)),
            "finished": bool(step_log.get("finished", False)),
        }
        with open(trace_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def finalize_artifacts(
        self, state: AssemblyState, *, final_metrics: dict, end_reason: str
    ) -> dict[str, Any]:
        """终局 ``summary.json``（benchmark v2 schema + end_reason/verifier 终判）。

        读取 barrier 的最后一回合做 postcondition 终判（与迁移前
        ``run()`` 的 ``verify_round(last_round_result)`` 口径一致），并合入
        task-metrics 累计指标。返回的字段并入 ``run_metrics.json``。
        """
        barrier = state.barrier
        verdict: dict[str, Any] = {}
        if barrier is not None and hasattr(barrier, "last_round_result"):
            from ai2thor_orch.verifier.verifier import verify_round

            verdict = verify_round(barrier.last_round_result(), self._contract)

        task_metrics: dict[str, Any] = {}
        if barrier is not None and hasattr(barrier, "get_task_metrics"):
            task_metrics = barrier.get_task_metrics()

        status = barrier.get_run_status() if barrier is not None else None
        summary: dict[str, Any] = {
            "run_id": final_metrics.get("run_id", state.run_id),
            "task_id": self._task_id,
            "scene": self._scene,
            "mode": self._mode,
            "num_agents": self._num_agents,
            "seed": self._seed,
            "metric_schema_version": 2,
            "max_steps": final_metrics.get("max_steps", state.max_steps),
            "rounds_completed": int(final_metrics.get("steps", 0)),
            "finished": bool(final_metrics.get("finished", False)),
            "stopped": bool(getattr(status, "stopped", False)),
            "stop_reason": str(getattr(status, "stop_reason", "")),
            "end_reason": end_reason,
            # Verifier 终判（postcondition 真值；与迁移前 summary.json 同名字段）
            "verified_completion": bool(verdict.get("verified_completion", False)),
            "coverage": float(verdict.get("coverage", 0.0)),
            "goal_coverage": float(verdict.get("goal_coverage", 0.0)),
            # Task-metrics 累计指标（task_metrics.py 快照）
            "interaction_coverage": float(
                task_metrics.get("interaction_coverage", 0.0)
            ),
            "transport_rate": float(task_metrics.get("transport_rate", 0.0)),
            "action_attempts": int(task_metrics.get("action_attempts", 0)),
            "successful_actions": int(task_metrics.get("successful_actions", 0)),
            "failed_actions": int(task_metrics.get("failed_actions", 0)),
            "action_success_rate": float(
                task_metrics.get("action_success_rate", 0.0)
            ),
            "timeout_count": int(task_metrics.get("timeout_count", 0)),
            "timeout_rounds": int(task_metrics.get("timeout_rounds", 0)),
            "balance": float(task_metrics.get("balance", 1.0)),
            "completed_subtask_count": int(
                task_metrics.get("completed_subtask_count", 0)
            ),
            "total_subtasks": int(task_metrics.get("total_subtasks", 0)),
            "per_agent_successful_actions": task_metrics.get(
                "per_agent_successful_actions", {}
            ),
            "elapsed_seconds": round(
                float(final_metrics.get("elapsed_seconds", 0.0)), 2
            ),
            "log_dir": str(state.exp_dir),
        }
        summary_path = Path(state.exp_dir) / "summary.json"
        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        logger.info(
            "Run summary written: %s (end_reason=%s verified=%s rounds=%d)",
            summary_path,
            end_reason,
            summary["verified_completion"],
            summary["rounds_completed"],
        )
        return {
            "verified_completion": summary["verified_completion"],
            "goal_coverage": summary["goal_coverage"],
            "summary_json": str(summary_path),
        }


__all__ = ["AI2ThorAssemblyHooks", "build_watchdog_config"]
