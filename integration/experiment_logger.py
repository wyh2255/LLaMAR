# -*- coding: utf-8 -*-
"""
experiment_logger.py -- Integration 实验日志记录器

参照 SAR/baselines/sar_logging.py 的 SARLogger 模式，为 MARoS x LLaMAR 集成实验
提供统一的日志记录功能。记录三类 CSV 日志：

  1. trajectory.csv  -- 每步轨迹指标（覆盖率、运输率、完成状态）
  2. agent_interactions.csv  -- 每个 Worker 智能体的工具调用详情
  3. router_log.csv  -- RouterAgent 的任务分解和 LLM 交互日志

输出目录结构：
  integration/results/{experiment_name}/
      actions/{n_agents}_agents/seed_{seed}/scene_{scene}/
          trajectory.csv
          agent_interactions.csv
          router_log.csv

参照 SARLogger 的 DataFrame 追加 + 立即写 CSV 模式。
"""
import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd


class IntegrationLogger:
    """Integration 实验日志记录器，参照 SARLogger 模式。

    使用 pandas DataFrame 追加 + 立即写 CSV 的方式记录实验数据。
    每次调用 log_* 方法后立即写入对应 CSV 文件，确保异常退出时不丢失已记录数据。

    属性:
        experiment_name: 实验名称（用于目录名）
        num_agents: 智能体数量
        scene: 场景编号
        seed: 随机种子
        results_path: 结果保存的根目录
        action_dir: actions 子目录路径
    """

    def __init__(
        self,
        experiment_name: str,
        num_agents: int,
        scene: int,
        seed: int,
    ):
        """初始化 IntegrationLogger，创建目录结构和空 DataFrame。

        Args:
            experiment_name: 实验名称，如 "integration_sar"
            num_agents: 智能体数量（1-6）
            scene: SAR 场景编号（1-5）
            seed: 随机种子
        """
        self.experiment_name = experiment_name
        self.num_agents = num_agents
        self.scene = scene
        self.seed = seed

        # 构建结果目录路径：integration/results/{experiment_name}/
        integration_dir = Path(__file__).parent.absolute()
        self.results_path = (
            integration_dir / "results" / experiment_name
        )

        # actions/{n_agents}_agents/seed_{seed}/scene_{scene}/
        self.action_dir = (
            self.results_path
            / "actions"
            / f"{num_agents}_agents"
            / f"seed_{seed}"
            / f"scene_{scene}"
        )

        # 创建目录
        os.makedirs(self.action_dir, exist_ok=True)

        # -- trajectory DataFrame --
        self._traj_cols = [
            "Step",
            "Action",
            "Success",
            "Coverage",
            "Transport Rate",
            "Finished",
        ]
        self._traj_df = pd.DataFrame(columns=self._traj_cols)

        # -- agent_interactions DataFrame --
        self._agent_cols = [
            "Step",
            "Agent",
            "Subtask",
            "LLM_Input",
            "LLM_Output",
            "Thinking",
            "Tool_Calls",
            "Action",
            "Observation",
            "Reasoning",
        ]
        self._agent_df = pd.DataFrame(columns=self._agent_cols)

        # -- router_log DataFrame --
        self._router_cols = [
            "Timestamp",
            "Phase",
            "LLM_Input",
            "LLM_Output",
            "Tool_Calls",
            "Subtasks_Dispatched",
        ]
        self._router_df = pd.DataFrame(columns=self._router_cols)

    # ── Trajectory logging ───────────────────────────────────────────────

    def log_step(
        self,
        step_num: int,
        actions: list,
        successes: list,
        coverage: float,
        transport_rate: float,
        finished: bool,
    ):
        """记录一步轨迹数据到 trajectory.csv。

        参照 SARLogger.log_step() 的模式：DataFrame 追加 + 立即写 CSV。

        Args:
            step_num: 当前步数
            actions: 各智能体的动作列表 ["NavigateTo(x)", "Move(Up)", ...]
            successes: 各智能体动作的成功状态 [True, False, ...]
            coverage: 环境覆盖率 (0.0-1.0)
            transport_rate: 资源运输率 (0.0-1.0)
            finished: 任务是否完成
        """
        row = pd.DataFrame(
            [[step_num, actions, successes, coverage, transport_rate, finished]],
            columns=self._traj_cols,
        )
        self._traj_df = pd.concat([self._traj_df, row], ignore_index=True)
        self._traj_df.to_csv(
            self.action_dir / "trajectory.csv", index=False
        )

    # ── Agent interaction logging ────────────────────────────────────────

    def log_agent_interaction(
        self,
        step: int,
        agent: str,
        subtask: str,
        llm_input: str = "",
        llm_output: str = "",
        thinking: str = "",
        tool_calls: list | None = None,
        action: str = "",
        observation: str = "",
        reasoning: str = "",
    ):
        """记录一次智能体工具调用到 agent_interactions.csv。

        每当 Worker 的 tool 函数执行 submit_action 后调用此方法，
        记录该次交互的完整上下文。

        Args:
            step: 当前步数
            agent: 智能体名称（如 "Alice"）
            subtask: 当前子任务描述
            llm_input: LLM 输入（prompt/messages 摘要，Worker 层暂不可用时为空）
            llm_output: LLM 输出（response 摘要，Worker 层暂不可用时为空）
            thinking: LLM 思考过程（DeepSeek thinking mode，Worker 层暂不可用时为空）
            tool_calls: 工具调用列表 [{"name": "...", "args": {...}}, ...]
            action: 实际执行的动作字符串（如 "NavigateTo(Fire_1)"）
            observation: 动作执行后的观测结果
            reasoning: 智能体的推理过程（Worker 层暂不可用时为空）
        """
        row = pd.DataFrame(
            [[
                step,
                agent,
                subtask,
                llm_input,
                llm_output,
                thinking,
                json.dumps(tool_calls or [], ensure_ascii=False),
                action,
                observation[:500] if observation else "",
                reasoning,
            ]],
            columns=self._agent_cols,
        )
        self._agent_df = pd.concat([self._agent_df, row], ignore_index=True)
        self._agent_df.to_csv(
            self.action_dir / "agent_interactions.csv", index=False
        )

    # ── Router logging ───────────────────────────────────────────────────

    def log_router(
        self,
        timestamp: str,
        phase: str,
        llm_input: str = "",
        llm_output: str = "",
        tool_calls: list | None = None,
        subtasks: list | None = None,
    ):
        """记录一次 RouterAgent 交互到 router_log.csv。

        由 SARCoordinator.submit_task() 调用，记录任务分解的输入/输出
        和所有 RouterAgent 工具调用。

        Args:
            timestamp: 时间戳字符串（ISO 格式）
            phase: 阶段标识（如 "task_submit", "task_complete"）
            llm_input: RouterAgent 的输入（任务描述或 LLM messages 摘要）
            llm_output: RouterAgent 的输出（最终文本结果）
            tool_calls: RouterAgent 执行的工具调用列表
            subtasks: 分发的子任务列表
        """
        row = pd.DataFrame(
            [[
                timestamp,
                phase,
                llm_input[:1000] if llm_input else "",
                llm_output[:1000] if llm_output else "",
                json.dumps(tool_calls or [], ensure_ascii=False),
                json.dumps(subtasks or [], ensure_ascii=False),
            ]],
            columns=self._router_cols,
        )
        self._router_df = pd.concat([self._router_df, row], ignore_index=True)
        self._router_df.to_csv(
            self.action_dir / "router_log.csv", index=False
        )

    # ── Summary ──────────────────────────────────────────────────────────

    def summarize(self) -> dict:
        """汇总本次实验的统计指标。

        参照 SARLogger.summarize() 的模式，从 trajectory.csv 计算汇总统计。

        Returns:
            dict: 包含 success_rate, coverage, transport_rate, total_steps,
                  balance, num_agents, elapsed 等指标
        """
        summary = {
            "experiment_name": self.experiment_name,
            "num_agents": self.num_agents,
            "scene": self.scene,
            "seed": self.seed,
        }

        if self._traj_df.empty:
            summary.update({
                "success_rate": 0.0,
                "coverage": 0.0,
                "transport_rate": 0.0,
                "total_steps": 0,
                "balance": 0.0,
            })
            return summary

        # 最后一步的指标
        last = self._traj_df.iloc[-1]
        summary["success_rate"] = float(last["Finished"])
        summary["coverage"] = float(last["Coverage"])
        summary["transport_rate"] = float(last["Transport Rate"])
        summary["total_steps"] = len(self._traj_df)

        # 多智能体平衡度（参照 SARLogger._get_balance_metric）
        summary["balance"] = self._compute_balance()

        # agent_interactions 统计
        if not self._agent_df.empty:
            summary["total_tool_calls"] = len(self._agent_df)
            summary["agents_logged"] = self._agent_df["Agent"].nunique()

        # router_log 统计
        if not self._router_df.empty:
            summary["router_calls"] = len(self._router_df)

        return summary

    def _compute_balance(self) -> float:
        """计算多智能体平衡度指标。

        参照 SARLogger._get_balance_metric()：统计每个智能体成功执行的动作数，
        返回 min/max 比值。过滤掉 NoOp 和 Idle。

        Returns:
            float: 平衡度（0.0-1.0），1.0 表示完美平衡
        """
        if self._traj_df.empty:
            return 0.0

        import collections
        agent_success_counts = collections.defaultdict(int)

        for _, row in self._traj_df.iterrows():
            actions = row["Action"]
            successes = row["Success"]
            if isinstance(actions, str):
                try:
                    actions = json.loads(actions)
                except (json.JSONDecodeError, TypeError):
                    actions = [actions]
            if isinstance(successes, str):
                try:
                    successes = json.loads(successes)
                except (json.JSONDecodeError, TypeError):
                    successes = [successes]

            for i, (act, suc) in enumerate(zip(actions, successes)):
                if act in ("NoOp", "Idle", "Done"):
                    continue
                if suc:
                    agent_success_counts[i] += 1

        if not agent_success_counts:
            return 0.0

        vals = list(agent_success_counts.values())
        return min(vals) / max(vals) if max(vals) > 0 else 0.0

    def close(self):
        """确保所有 CSV 文件已写入（实际上每次 log_* 调用后已立即写入）。

        此方法主要作为显式关闭的信号，在实验结束时调用。
        """
        # CSV 已在每次 log_* 调用后立即写入
        # 这里可以做最终的汇总写入
        summary = self.summarize()
        summary_path = self.action_dir / "summary.json"
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

    def get_log_dir(self) -> str:
        """返回日志保存目录的绝对路径。

        Returns:
            str: 日志目录路径
        """
        return str(self.action_dir)
