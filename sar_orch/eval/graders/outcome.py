from sar_orch.eval.dataset import EpisodeDataset
from sar_orch.eval.graders.base import GradeResult


def _parse_csv_list(value: str):
    if not value or value == "[]":
        return []
    try:
        return eval(value)
    except Exception:
        return []


#: 论文 §5 Metrics 中 Balance 定义里的稳定项，用于避免除零。
_BALANCE_EPSILON = 1e-4

#: 不计入"成功高层动作"的空动作。原始 LLaMAR 参考实现过滤 ["Done", "Idle"]
#: （AI2-THOR 词汇），SAR 里等价的空动作是 NoOp；三者全部保留以兼容两边数据。
_BALANCE_SKIP_ACTIONS = ("NoOp()", "NoOp", "Idle", "Done")


def episode_agent_count(episode: EpisodeDataset) -> int:
    """本 episode 的智能体数 n。

    优先取 metadata（权威值），否则回退到轨迹里 Actions 列的最大长度。
    n 必须覆盖**全部**智能体，否则从未成功过的智能体会被漏掉，
    balance 被高估（例如 2 智能体中一个 0 次成功、另一个 5 次，
    漏算会得到 5/5=1.0，而论文定义应为 0/5≈0.0）。
    """
    meta = episode.metadata or {}
    for key in ("agent_count", "agents", "num_agents"):
        val = meta.get(key)
        if isinstance(val, int) and val > 0:
            return val
    if episode.agent_names:
        return len(episode.agent_names)
    return max(
        (len(sr.actions) for sr in episode.steps.values()),
        default=0,
    )


def compute_balance(episode: EpisodeDataset) -> float:
    """论文 §5 的 Balance 指标。

    B := min{s_1,...,s_n} / (max{s_1,...,s_n} + eps)，其中 s_i 为第 i 个
    智能体成功执行的高层动作数，n 为本 episode 的智能体总数，eps=1e-4。

    B=0 表示至少一个智能体没有任何成功动作；B≈1 表示各智能体贡献相同。
    """
    n = episode_agent_count(episode)
    if n <= 0:
        return 0.0

    # 零填充所有 n 个智能体：从未成功的智能体必须以 0 计入 min。
    agent_success_counts: dict[int, int] = {i: 0 for i in range(n)}
    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        for i, (act, suc) in enumerate(zip(sr.actions, sr.successes)):
            if i >= n:
                # 轨迹比 metadata 声明的智能体更多：按实际数据扩展，避免漏算。
                agent_success_counts.setdefault(i, 0)
            if act in _BALANCE_SKIP_ACTIONS:
                continue
            if suc:
                agent_success_counts[i] = agent_success_counts.get(i, 0) + 1

    vals = list(agent_success_counts.values())
    return min(vals) / (max(vals) + _BALANCE_EPSILON)


#: 向后兼容的私有别名（历史调用方使用 _compute_balance）。
_compute_balance = compute_balance


def grade_outcome(episode: EpisodeDataset) -> list[GradeResult]:
    results: list[GradeResult] = []

    last = episode.last_step
    if last is None:
        return results

    final_coverage = last.coverage
    final_transport_rate = last.transport_rate
    finished = last.finished
    total_steps = max(episode.steps.keys()) if episode.steps else 0
    end_reason = last.end_reason

    # 协调器下发的 dispatch 计数（来自 subtasks.csv）。
    # 注意：这**不是**环境 checker 的子任务数 —— subtasks.csv 记录的是
    # coordinator 分派给 worker 的自然语言任务（scene 1 两个 agent 各 1 条，
    # 共 2 条），而 checker.subtasks 是 15 个可判定的原子子任务。
    # 论文 TR 的分母是后者；这里两个字段仅用于观测调度行为，不参与效率计算。
    dispatch_completed_count = sum(
        1 for s in episode.subtask_records if s.status == "completed"
    )
    dispatch_count = len(episode.subtask_records)

    # token aggregation
    token_usage_rows = episode.token_usage_rows
    total_tokens = 0
    agent_tokens: dict[str, int] = {}
    map_tokens = 0
    map_agent_tokens = 0
    map_summarizer_tokens = 0
    for row in token_usage_rows:
        agent = row.get("Agent", "")
        tt = int(row.get("TotalTokens", 0))
        total_tokens += tt
        agent_tokens[agent] = agent_tokens.get(agent, 0) + tt
    map_agent_tokens = agent_tokens.get("MapAgent", 0)
    map_summarizer_tokens = agent_tokens.get("MapSummarizer", 0)
    map_tokens = map_agent_tokens + map_summarizer_tokens

    # 环境 checker 真实判定完成的子任务数（trajectory.csv 的
    # CompletedSubtasksDelta 逐步累加）。这是唯一与论文 TR 同源的完成量：
    # TR == trajectory_completed / len(checker.subtasks)。
    trajectory_completed = sum(
        len(sr.completed_subtasks_delta) for sr in episode.steps.values()
    )

    # checker 的子任务总数（论文 TR 的分母）。它没有被任何产物直接记录，
    # 但 TR == trajectory_completed / total 成立，故可由二者反推。
    # TR 为 0 时无法反推，返回 None 而不是猜一个数。
    checker_subtask_total: int | None = None
    if final_transport_rate and trajectory_completed:
        checker_subtask_total = round(trajectory_completed / final_transport_rate)

    # 每步产出的已完成子任务数。分子必须用 checker 的真实完成量，
    # 不能用 total_subtasks —— 后者是协调器下发的 dispatch 条数（见下方
    # dispatch_count），与环境子任务是两个不同概念，用它会把本指标压成
    # 一个与"效率"无关的数（例如 2/30 而非 13/30）。
    step_efficiency = trajectory_completed / total_steps if total_steps > 0 else 0.0

    # 每完成一个子任务平均消耗的 token（越低越好）。分母与 step_efficiency
    # 的分子同源，保证两个效率指标口径一致。
    token_efficiency = (
        total_tokens / trajectory_completed if trajectory_completed > 0 else 0.0
    )

    # map overhead ratio
    map_overhead = map_tokens / total_tokens if total_tokens > 0 else 0.0

    # balance
    balance = _compute_balance(episode)

    # progress curve data from CompletedSubtasksDelta
    progress_curve = []
    cumulative = 0
    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        delta = sr.completed_subtasks_delta
        cumulative += len(delta)
        progress_curve.append(
            {"step": step_num, "delta": len(delta), "cumulative": cumulative}
        )

    episode_result = GradeResult(
        grader="OutcomeGrader",
        level="episode",
        passed=None,
        score=None,
        detail={
            "final_coverage": final_coverage,
            "final_transport_rate": final_transport_rate,
            "finished": finished,
            "total_steps": total_steps,
            "end_reason": end_reason,
            # 环境 checker 的真实完成量与总数（与论文 TR 同源）
            "completed_subtasks_trajectory": trajectory_completed,
            "checker_subtask_total": checker_subtask_total,
            # 协调器 dispatch 计数，非环境子任务 —— 勿用于效率/完成率
            "dispatch_count": dispatch_count,
            "dispatch_completed_count": dispatch_completed_count,
            "step_efficiency": step_efficiency,
            "total_tokens": total_tokens,
            "agent_tokens": agent_tokens,
            "map_tokens": map_tokens,
            "map_agent_tokens": map_agent_tokens,
            "map_summarizer_tokens": map_summarizer_tokens,
            "map_overhead_ratio": map_overhead,
            "token_efficiency": token_efficiency,
            "balance": balance,
            "progress_curve": progress_curve,
        },
        evidence_ref="trajectory.csv,summary.csv,token_usage.csv,subtasks.csv",
    )
    results.append(episode_result)

    return results


OUTCOME_GRADER_NAME = "OutcomeGrader"
