"""论文口径指标的保真度回归测试。

对应 LLaMAR 论文 §5 Metrics 的 5 个指标（SR / TR / C / B / L）。
重点锁住 Balance —— 它曾被实现成 `1.0 if finished else transport_rate`
这种与论文毫无关系的占位符，这里用可执行断言把定义钉住。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from sar_orch.aggregate import aggregate
from sar_orch.eval.aggregate import (
    _numeric_stats,
    _t_ppf,
    aggregate_group,
    clopper_pearson_interval,
    write_aggregate_report_md,
)
from sar_orch.eval.dataset import load_episode
from sar_orch.eval.graders.outcome import (
    _BALANCE_EPSILON,
    compute_balance,
    episode_agent_count,
)

_TRAJ_HEADER = [
    "Step",
    "Actions",
    "Successes",
    "Observations",
    "Coverage",
    "TransportRate",
    "Finished",
    "TimeoutAgents",
    "CompletedSubtasksDelta",
    "EndReason",
]


def _write_run(
    run_dir: Path,
    steps: list[tuple[list[str], list[bool]]],
    *,
    agent_count: int | None = None,
    coverage: float = 0.0,
    transport_rate: float = 0.0,
    finished: bool = False,
) -> Path:
    """造一个最小 run 目录：trajectory.csv + metadata.json。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "trajectory.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_ALL)
        writer.writerow(_TRAJ_HEADER)
        for idx, (actions, successes) in enumerate(steps, start=1):
            writer.writerow(
                [
                    idx,
                    repr(actions),
                    repr(successes),
                    "",
                    coverage,
                    transport_rate,
                    str(finished and idx == len(steps)),
                    "[]",
                    "[]",
                    "",
                ]
            )
    meta: dict = {"scene": 1, "seed": 42}
    if agent_count is not None:
        meta["agent_count"] = agent_count
    (run_dir / "metadata.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    return run_dir


def _set_deltas(run_dir: Path, deltas: dict[int, list[str]]) -> None:
    """重写 trajectory.csv 的 CompletedSubtasksDelta 列。"""
    path = run_dir / "trajectory.csv"
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_ALL)
        writer.writerow(_TRAJ_HEADER)
        for row in rows:
            row["CompletedSubtasksDelta"] = repr(deltas.get(int(row["Step"]), []))
            writer.writerow([row[c] for c in _TRAJ_HEADER])


def _outcome_detail(run_dir: Path) -> dict:
    """跑 OutcomeGrader，返回 episode 级 detail。"""
    from sar_orch.eval.graders.outcome import grade_outcome

    results = grade_outcome(load_episode(run_dir))
    return results[0].detail


# ── Balance ───────────────────────────────────────────────────────────────


def test_balance_is_min_over_max_plus_epsilon(tmp_path: Path):
    """B := min(s_i)/(max(s_i)+eps)，eps=1e-4。"""
    run = _write_run(
        tmp_path / "run",
        steps=[
            (["NavigateTo(FireA)", "NavigateTo(FireB)"], [True, True]),
            (["UseSupply(FireA, Water)", "Move(Up)"], [True, False]),
            (["UseSupply(FireA, Water)", "Move(Up)"], [True, False]),
        ],
        agent_count=2,
    )
    # agent0: 3 次成功；agent1: 1 次成功
    assert compute_balance(load_episode(run)) == pytest.approx(
        1 / (3 + _BALANCE_EPSILON)
    )


def test_balance_is_zero_when_an_agent_never_succeeds(tmp_path: Path):
    """论文：balance=0 表示至少一个智能体没有任何成功高层动作。

    这是最容易被写错的一条 —— 若只统计"出现过成功动作"的智能体，
    零成功者会被静默漏掉，2 智能体会算出 1.0（"完美均衡"）。
    """
    run = _write_run(
        tmp_path / "run",
        steps=[
            (["NavigateTo(FireA)", "Move(Up)"], [True, False]),
            (["UseSupply(FireA, Water)", "Move(Up)"], [True, False]),
        ],
        agent_count=2,
    )
    assert compute_balance(load_episode(run)) == 0.0


def test_balance_counts_all_agents_from_metadata_not_just_active_ones(tmp_path: Path):
    """智能体总数 n 取自 metadata，第 3 个智能体全程失败也必须计入。"""
    run = _write_run(
        tmp_path / "run",
        steps=[
            (
                ["NavigateTo(FireA)", "NavigateTo(FireB)", "Move(Up)"],
                [True, True, False],
            ),
        ],
        agent_count=3,
    )
    assert compute_balance(load_episode(run)) == 0.0


def test_balance_perfect_when_all_agents_equal(tmp_path: Path):
    run = _write_run(
        tmp_path / "run",
        steps=[
            (["NavigateTo(FireA)", "NavigateTo(FireB)"], [True, True]),
            (["GetSupply(ReservoirYork)", "GetSupply(ReservoirUtah)"], [True, True]),
        ],
        agent_count=2,
    )
    balance = compute_balance(load_episode(run))
    assert balance == pytest.approx(2 / (2 + _BALANCE_EPSILON))
    assert balance < 1.0  # epsilon 使其严格小于 1


def test_balance_excludes_noop_actions(tmp_path: Path):
    """NoOp 是空动作，不算"成功高层动作"。"""
    run = _write_run(
        tmp_path / "run",
        steps=[
            (["NavigateTo(FireA)", "NoOp()"], [True, True]),
            (["NavigateTo(FireB)", "NoOp()"], [True, True]),
        ],
        agent_count=2,
    )
    # agent1 全是 NoOp → 0 次成功高层动作 → balance 0
    assert compute_balance(load_episode(run)) == 0.0


def test_balance_never_uses_finished_or_transport_rate_placeholder(tmp_path: Path):
    """回归：balance 不得退化成 `1.0 if finished else transport_rate`。

    构造一个 finished=True、transport_rate=1.0，但两智能体贡献严重失衡的
    episode。占位符实现会给出 1.0；正确实现必须给出接近 1/4 的值。
    """
    run = _write_run(
        tmp_path / "run",
        steps=[
            (["NavigateTo(FireA)", "NavigateTo(FireB)"], [True, True]),
            (["UseSupply(FireA, Water)", "Move(Up)"], [True, False]),
            (["UseSupply(FireA, Water)", "Move(Up)"], [True, False]),
            (["Carry(LostPersonTimmy)", "Move(Up)"], [True, False]),
        ],
        agent_count=2,
        transport_rate=1.0,
        coverage=1.0,
        finished=True,
    )
    balance = compute_balance(load_episode(run))
    assert balance == pytest.approx(1 / (4 + _BALANCE_EPSILON))
    assert balance < 0.9


def test_tsv_aggregate_balance_matches_grader(tmp_path: Path):
    """standalone TSV 聚合器必须复用同一个 balance 实现，而非占位符。"""
    seed_dir = tmp_path / "benchmark" / "scene_1" / "agents_2" / "seed_42"
    _write_run(
        seed_dir,
        steps=[
            (["NavigateTo(FireA)", "NavigateTo(FireB)"], [True, True]),
            (["UseSupply(FireA, Water)", "Move(Up)"], [True, False]),
        ],
        agent_count=2,
        transport_rate=1.0,
        finished=True,
    )
    (seed_dir / "result.json").write_text(
        json.dumps(
            {
                "finished": True,
                "steps": 2,
                "coverage": 1.0,
                "transport_rate": 1.0,
                "end_reason": "",
                "log_dir": str(seed_dir),
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "out.tsv"
    aggregate(str(tmp_path / "benchmark"), str(output))
    rows = list(csv.DictReader(output.open(newline="", encoding="utf-8"), delimiter="\t"))

    expected = 1 / (2 + _BALANCE_EPSILON)
    assert float(rows[0]["balance"]) == pytest.approx(expected)
    # 占位符会给出 1.0（finished=True）——确保不是它
    assert float(rows[0]["balance"]) != pytest.approx(1.0)


# ── Success Rate + 置信区间 ────────────────────────────────────────────────


def _report(finished: bool, **episode) -> dict:
    ep = {"finished": finished}
    ep.update(episode)
    return {
        "metadata": {"scene": 1, "agents": 2, "seed": 0},
        "run_dir": "r",
        "episode": ep,
    }


def test_success_rate_is_reported_with_clopper_pearson_ci():
    reports = [
        _report(True, coverage=1.0, transport_rate=1.0, balance=0.9, steps=10),
        _report(False, coverage=0.5, transport_rate=0.4, balance=0.5, steps=30),
        _report(True, coverage=0.8, transport_rate=0.9, balance=0.7, steps=20),
    ]
    group = aggregate_group(reports)
    sr = group["success_rate"]
    assert sr["mean"] == pytest.approx(2 / 3)
    assert sr["successes"] == 2 and sr["n"] == 3
    assert sr["ci_method"] == "clopper-pearson"
    # scipy/statsmodels 参考值：proportion_confint(2, 3, 0.05, method="beta")
    assert sr["ci95_low"] == pytest.approx(0.09429, abs=1e-3)
    assert sr["ci95_high"] == pytest.approx(0.99157, abs=1e-3)
    # SR 数值上等于 pass@1
    assert sr["mean"] == pytest.approx(group["pass_at_k"]["1"])


def test_clopper_pearson_matches_statsmodels_reference():
    """对齐 meta/result_analysis/confidence_intervals.py 的 get_CP_interval。"""
    for successes, n, low, high in [
        (7, 10, 0.34755, 0.93295),
        (0, 10, 0.0, 0.30850),
        (10, 10, 0.69150, 1.0),
        (3, 20, 0.03207, 0.37893),
    ]:
        got_low, got_high = clopper_pearson_interval(successes, n)
        assert got_low == pytest.approx(low, abs=1e-3)
        assert got_high == pytest.approx(high, abs=1e-3)


def test_clopper_pearson_handles_empty_sample():
    assert clopper_pearson_interval(0, 0) == (None, None)


def test_t_quantile_matches_scipy_reference():
    """对齐 scipy.stats.t.ppf(0.975, df)。"""
    for df, expected in {1: 12.7062, 2: 4.3027, 9: 2.2622, 29: 2.0452}.items():
        assert _t_ppf(0.975, df) == pytest.approx(expected, abs=1e-2)


def test_numeric_stats_reports_t_interval():
    stats = _numeric_stats([0.1, 0.2, 0.3, 0.4, 0.5])
    assert stats["mean"] == pytest.approx(0.3)
    # scipy 参考：mean ± t.ppf(0.975, 4) * sem = 0.3 ± 0.19632
    assert stats["ci95_low"] == pytest.approx(0.10368, abs=1e-3)
    assert stats["ci95_high"] == pytest.approx(0.49632, abs=1e-3)


def test_numeric_stats_degenerate_samples():
    single = _numeric_stats([0.5])
    assert single["ci95_low"] == 0.5 and single["ci95_high"] == 0.5
    empty = _numeric_stats([])
    assert empty["ci95_low"] is None and empty["ci95_high"] is None


def test_agent_count_ignores_non_env_agents(tmp_path: Path):
    """智能体数必须取 metadata.agent_count，不能用 agent_names 的长度。

    agent_names 里含 MapAgent / MapSummarizer 等非环境角色。若用它当 balance
    的分母 n，这些永不行动的"智能体"会被当成零成功者，把 balance 压成 0。
    """
    run = _write_run(
        tmp_path / "run",
        steps=[(["NavigateTo(FireA)", "NavigateTo(FireB)"], [True, True])],
        agent_count=2,
    )
    episode = load_episode(run)
    # 模拟 dataset 把辅助角色也收进 agent_names 的真实情况
    episode.agent_names = ["Alice", "Bob", "MapAgent", "MapSummarizer"]
    assert episode_agent_count(episode) == 2
    # 1 步，两个 agent 各 1 次成功 → 1/(1+eps)，而非被虚构角色压成 0
    assert compute_balance(episode) == pytest.approx(1 / (1 + _BALANCE_EPSILON))


def test_agent_count_falls_back_to_trajectory_width(tmp_path: Path):
    """metadata 缺 agent_count 时，回退到轨迹里 Actions 的宽度。"""
    run = _write_run(
        tmp_path / "run",
        steps=[(["NavigateTo(FireA)", "NavigateTo(FireB)", "Move(Up)"], [True, True, True])],
        agent_count=None,
    )
    episode = load_episode(run)
    episode.agent_names = []
    assert episode_agent_count(episode) == 3


# ── 自定义指标：checker 口径 vs dispatch 口径 ──────────────────────────────


def test_step_efficiency_uses_checker_completions_not_dispatch_count(tmp_path: Path):
    """step_efficiency 分子必须是 checker 真实完成量，不能是 dispatch 条数。

    subtasks.csv 记的是协调器下发的自然语言任务（常仅 2 条），
    checker 的原子子任务是十几个。混用会把该指标压成与"效率"无关的数。
    """
    run = tmp_path / "run"
    _write_run(
        run,
        steps=[
            (["NavigateTo(FireA)", "NavigateTo(FireB)"], [True, True]),
            (["UseSupply(FireA, Water)", "UseSupply(FireB, Water)"], [True, True]),
        ],
        agent_count=2,
        transport_rate=0.5,
    )
    # 4 个 checker 完成项分布在 2 步
    _set_deltas(run, {1: ["NavigateTo(FireA)", "NavigateTo(FireB)"], 2: ["EndFire(FireA)", "EndFire(FireB)"]})
    # dispatch 只有 1 条
    (run / "subtasks.csv").write_text(
        '"RunID","Step","SubtaskID","Status","AssignedTo","Subtask"\n'
        '"r","0","dispatch-1","assigned","Alice","explore"\n',
        encoding="utf-8",
    )

    detail = _outcome_detail(run)
    assert detail["completed_subtasks_trajectory"] == 4
    assert detail["dispatch_count"] == 1
    # 4 completions / 2 steps = 2.0；若误用 dispatch_count 会得 0.5
    assert detail["step_efficiency"] == pytest.approx(2.0)


def test_checker_subtask_total_is_derived_from_transport_rate(tmp_path: Path):
    """TR 的分母可由 completed/TR 反推，用于报告里同源的 x/y 显示。"""
    run = tmp_path / "run"
    _write_run(
        run,
        steps=[(["NavigateTo(FireA)", "NavigateTo(FireB)"], [True, True])],
        agent_count=2,
        transport_rate=13 / 15,
    )
    _set_deltas(run, {1: [f"Sub{i}" for i in range(13)]})
    detail = _outcome_detail(run)
    assert detail["checker_subtask_total"] == 15
    assert detail["completed_subtasks_trajectory"] == 13


def test_checker_subtask_total_is_none_when_transport_rate_zero(tmp_path: Path):
    """TR=0 时无法反推分母，必须返回 None 而不是猜一个数。"""
    run = tmp_path / "run"
    _write_run(
        run,
        steps=[(["Move(Up)", "Move(Up)"], [True, True])],
        agent_count=2,
        transport_rate=0.0,
    )
    assert _outcome_detail(run)["checker_subtask_total"] is None


def test_token_efficiency_shares_denominator_with_step_efficiency(tmp_path: Path):
    """两个效率指标必须同源：token/completions 与 completions/steps。"""
    run = tmp_path / "run"
    _write_run(
        run,
        steps=[(["NavigateTo(FireA)", "NavigateTo(FireB)"], [True, True])],
        agent_count=2,
        transport_rate=0.5,
    )
    _set_deltas(run, {1: ["A", "B", "C", "D"]})
    (run / "token_usage.csv").write_text(
        '"Agent","TotalTokens"\n"Alice","400"\n"Bob","400"\n', encoding="utf-8"
    )
    detail = _outcome_detail(run)
    assert detail["completed_subtasks_trajectory"] == 4
    assert detail["token_efficiency"] == pytest.approx(800 / 4)
    assert detail["step_efficiency"] == pytest.approx(4 / 1)


# ── Average steps (L) ─────────────────────────────────────────────────────


def test_paper_max_steps_is_30():
    """论文 §5：L=30，超出即判定失败。"""
    from sar_orch.benchmark import PAPER_MAX_STEPS as bench_l

    assert bench_l == 30


def test_benchmark_and_experiment_agree_on_l():
    """benchmark 与 experiment 各自定义 L，必须一致（前者不能 import 后者）。"""
    import ast

    from sar_orch.benchmark import PAPER_MAX_STEPS as bench_l

    src = Path(__file__).resolve().parent.parent / "sar_orch" / "experiment.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    values = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == "PAPER_MAX_STEPS"
        if isinstance(node.value, ast.Constant)
    ]
    assert values == [bench_l]


def test_markdown_report_contains_paper_metric_table(tmp_path: Path):
    reports = [
        _report(True, coverage=1.0, transport_rate=1.0, balance=0.9, steps=10),
        _report(False, coverage=0.5, transport_rate=0.4, balance=0.5, steps=30),
    ]
    agg = {
        "root_dir": str(tmp_path),
        "generated_at": "now",
        "valid_run_dirs": 2,
        "total_run_dirs_found": 2,
        "skipped_dirs": [],
        "groups": [aggregate_group(reports)],
    }
    out = tmp_path / "aggregate_report.md"
    write_aggregate_report_md(agg, out)
    text = out.read_text(encoding="utf-8")
    assert "论文口径指标" in text
    assert "SR (95% CI)" in text
    assert "Clopper-Pearson" in text
